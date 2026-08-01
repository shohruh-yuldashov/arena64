"""`ProfileSearchService` — find players, then render them exactly as a
profile page would.

The second consumer of `PublicProfileComposer`, and the reason it exists.
This service contributes **no view logic at all**: it resolves a term into
identities through `users`' published searcher and hands them to the same
composer `GET /profiles/{username}` uses. A64-013.1 requires that "search
results use the same public representation as profile pages", and this file
is what makes that structural — there is no branch here that could render a
player differently, because there is no rendering here.

Read-only. Opens no transaction.

## What this service actually decides

Two things, and neither belongs anywhere else.

**Who is excluded.** The caller's own account never appears in their own
results. That is a search-quality decision on a feature whose entire purpose
is finding *other* people — but it is also the exclusion mechanism blocking
will use, exercised from the first release rather than reserved for later.
See `UserSearchQuery.exclude_player_ids` on why a set that is always empty
would have been the worse design.

**What is recorded.** A64-013.1 asks for the query *length*, the result
count and the execution time, and forbids logging the raw term. That last
prohibition is the interesting one: a search log is a record of who looked
for whom, which on a platform with private profiles is more sensitive than
anything the search returns. The three numbers answer every operational
question — is the endpoint slow, is somebody enumerating, are terms
degenerate — without recording a single thing anybody typed.

## Why the term is parsed here rather than at the edge

`SearchTerm.parse` runs in this service, not in the request schema, even
though the schema could enforce the same bounds and would produce a tidier
FastAPI error.

Because the rules are the domain's. A non-HTTP caller — a future admin
tool, a Celery job reconciling handles — must hit the same minimum length
and the same wildcard refusal, and a rule that lives in a Pydantic field is
a rule that applies to one transport. The schema still declares the bounds
so they appear in the generated documentation; it is the second lock, not
the first, exactly as `ProfileUpdateRequest` declares lengths the domain
validators also enforce.
"""

import logging
import time
from uuid import UUID

from app.modules.profiles.application.ports import BlockedPlayersProvider
from app.modules.profiles.application.services.profile_composer import PublicProfileComposer
from app.modules.profiles.domain.profile import PublicProfile
from app.modules.profiles.domain.search import ProfileSearchResults
from app.modules.users.domain.search import SearchTerm
from app.modules.users.public import PublicProfileSearcher, UserSearchQuery

logger = logging.getLogger(__name__)


class ProfileSearchService:
    def __init__(
        self,
        *,
        searcher: PublicProfileSearcher,
        composer: PublicProfileComposer,
        blocked_players: BlockedPlayersProvider,
    ) -> None:
        self._searcher = searcher
        self._composer = composer
        # A64-013.5. Typed as the port, so this service cannot learn that a
        # `friends` module exists — it asks who to exclude and never why.
        self._blocked_players = blocked_players

    async def search(
        self,
        raw_term: str,
        *,
        limit: int,
        cursor: str | None,
        viewer_id: UUID,
    ) -> ProfileSearchResults:
        """One page of players matching `raw_term`, ranked and composed.

        `viewer_id` is the authenticated caller. It is **not** a filter a
        client supplies and could not be: the endpoint is authenticated and
        takes the id from the access token, so there is no way to search as
        somebody else — which matters because the exclusion set derived from
        it will, once `friends` ships, encode who has blocked whom.

        Raises `InvalidSearchTerm` (422) for a term that fails the domain
        rules and `InvalidSearchCursor` (422) for a cursor that is malformed
        or belongs to a different term. Never raises for a term nobody
        matches — an empty page is the correct answer, and a 404 would tell
        a caller whether anybody by that name exists.
        """
        term = SearchTerm.parse(raw_term)

        # Fetched before the query rather than filtered after it: a blocked
        # player must not occupy a row of the page, and post-filtering a
        # keyset page would return short pages whose cursor skipped people.
        blocked = await self._blocked_players.blocked_ids_for(viewer_id)

        # `perf_counter`, not the injected `Clock`. AD-07 governs *domain*
        # time — the instants that end up in records and drive rules — and
        # this is a stopwatch over a code path, which is neither. A test
        # asserting on a duration would be asserting on the machine it runs
        # on, so nothing does; the number exists for an operator watching a
        # percentile, and a monotonic counter is the only correct source for
        # one (it does not go backwards when NTP adjusts the wall clock).
        started = time.perf_counter()

        page = await self._searcher.search_public_profiles(
            UserSearchQuery(
                term=term.value,
                limit=limit,
                cursor=cursor,
                # The searcher, plus everybody they cannot interact with.
                #
                # **This is the whole of A64-013.5's search integration**,
                # and the parameter it uses is the one A64-013.1 built for
                # it — no second filtering mechanism, no `WHERE NOT IN`
                # written a second time, and the SQL branch that applies it
                # has been exercised on every request since that task
                # because the searcher's own id was always in the set.
                #
                # Symmetric: a player excluded here may have blocked the
                # searcher or been blocked by them, and neither can tell
                # which — a one-directional exclusion would make the
                # asymmetry itself the signal BL-1 withholds.
                exclude_player_ids=frozenset({viewer_id}) | blocked,
            )
        )

        # **`viewer_id`, not the default.** A64-013.4 fixed this: search
        # composed as an anonymous viewer, so a player searching for a
        # *friend* saw that friend's `friends`-scoped fields hidden — the
        # opposite of what the setting says, and inconsistent with the
        # friend list, which showed them.
        #
        # It was invisible until A64-013.3 made `FRIENDS` reachable, because
        # before that every viewer resolved to `STRANGER` anyway. This is
        # the query A64-013.4 means by "`friend_ids_among()` is now a hot
        # path": one per search page, and correct.
        #
        # No `known_relationship` here, deliberately — a search page mixes
        # friends and strangers, so it is the one list that must genuinely
        # resolve.
        profiles = await self._composer.compose_many(page.identities, viewer_id=viewer_id)

        elapsed_ms = (time.perf_counter() - started) * 1000

        # **Length, count, duration. Never the term, and never who was
        # found.** A64-013.1's logging contract, and every field here is a
        # number for a reason — see this module's docstring.
        #
        # `viewer_id` is included because the operational question this log
        # answers is "is one account enumerating", which needs a subject.
        # It records who *searched*, never what for or who was returned, so
        # it cannot be reassembled into a social graph (services.md §8.5).
        logger.info(
            "user_search_completed",
            extra={
                "user_id": str(viewer_id),
                "term_length": term.length,
                "result_count": len(profiles),
                # A count, never the ids. Who a player has blocked is the
                # one edge on this platform the other party must never learn
                # about (BL-1), and a log naming them would put it somewhere
                # with broader read access than the row (services.md §8.5).
                "excluded_count": len(blocked),
                "has_more": page.has_more,
                "elapsed_ms": round(elapsed_ms, 2),
            },
        )

        return ProfileSearchResults(
            profiles=tuple(profiles),
            next_cursor=page.next_cursor,
        )


__all__ = ["ProfileSearchService", "PublicProfile"]
