"""`CachedSocialGraphReader` — the `friends:v1:` cache, applied.

A **decorator** over `SocialGraphReaderService` rather than a cache inside
the repository, and the placement is the design:

  - a repository's job is "how to fetch from storage" (repositories.md §2),
    and Redis is not this module's storage — PostgreSQL is. A repository
    that consulted a cache would be a repository with two sources of truth;
  - the composition root chooses cached or uncached by swapping one object,
    so `FRIENDS_CACHE_ENABLED=false` is a wiring change and not a branch
    inside a query;
  - and every consumer sees `friends.public.SocialGraphReader`, so nothing
    outside this module can tell whether an answer came from Redis.

## Correctness over hit rate

A64-013.6 says so outright, and two decisions follow.

**A miss is never an error.** Every failure inside the cache — unreachable,
slow, malformed — surfaces here as a miss and falls through to the database,
which is where every one of these answers came from before this task.

**`friend_ids_among` is answered from the whole friend set**, intersected in
Python, rather than from a per-page cache entry. That is what makes one key
per player serve every page; see `infrastructure.cache.keys` on why the
alternative has a hit rate near zero.

**A miss costs exactly one query**, and it is the same one whose result is
stored: `friend_ids_for`. The obvious alternative — answer the narrow
question from `friend_ids_among`, then read the whole set to populate the
entry — was written first and was wrong. It issues *two* reads per miss, and
under `FRIENDS_CACHE_ENABLED=false` it issues both on every request forever,
because a cache that stores nothing never stops missing. Turning the cache
off must not make the platform slower than it was before the cache existed.

The whole set is what the entry holds, so reading the whole set is not
overhead — it is the read. The narrow query stays on the repository because
that is what the uncached reader still uses.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from app.modules.friends.application.ports import SocialGraphCache, SocialGraphEntry
from app.modules.friends.application.services.social_graph_reader import (
    SocialGraphReaderService,
)

logger = logging.getLogger(__name__)


class CachedSocialGraphReader:
    """`SocialGraphReader`, backed by `friends:v1:`.

    Satisfies the same published port as the service it wraps, so the
    composition root swaps one for the other and no consumer changes.
    """

    def __init__(self, reader: SocialGraphReaderService, cache: SocialGraphCache) -> None:
        self._reader = reader
        self._cache = cache

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """The block set, from cache when present.

        **The highest-value entry in the namespace.** This read runs on
        every profile composition and every search — A64-013.4 already
        called `friend_ids_among` a hot path, and A64-013.5 put this one
        beside it. It is also the cheapest to cache correctly: the set is
        per-viewer, so one key answers a page of any length, and it changes
        only when the viewer blocks or unblocks.
        """
        cached = await self._cache.get_ids(player_id, SocialGraphEntry.BLOCKED)
        if cached is not None:
            return cached

        blocked = await self._reader.blocked_ids_for(player_id)
        await self._cache.put_ids(player_id, SocialGraphEntry.BLOCKED, blocked)
        return blocked

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        """The friends among `others`, intersected against the cached set.

        On a **hit** this is pure set arithmetic and costs no query at all,
        which is the point: composition asks this for every page on the
        platform.

        On a **miss** it reads the whole friend set — one indexed,
        index-only query — stores it, and answers from it. One query, which
        is what the uncached reader costs too, so an inert cache is never a
        regression. See this module's docstring on the two-query shape this
        replaced.

        The store is awaited rather than fired and forgotten: an unawaited
        task would outlive the request-scoped database session, and
        `SocialGraphCache` promises never to raise and to be bounded by its
        own timeout, so awaiting it cannot hang the request either.
        """
        if not others:
            # No query and no cache read: an empty page is the ordinary
            # result of a search nobody matched.
            return set()

        cached = await self._cache.get_ids(player_id, SocialGraphEntry.FRIENDS)
        if cached is None:
            cached = await self._reader.friend_ids_for(player_id)
            await self._cache.put_ids(player_id, SocialGraphEntry.FRIENDS, cached)

        return {other for other in others if other in cached}
