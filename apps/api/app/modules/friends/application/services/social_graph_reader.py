"""`SocialGraphReaderService` — the implementation behind
`friends.public.ports.SocialGraphReader`.

The one adapter `profiles` is handed, and the reason it is one rather than
two: the published port has two methods because relationship resolution asks
both on every composition — a block outranks a friendship, so an answer
computed from only the friendship half is wrong — and a protocol is
satisfied by an object, not by a pair of them.

## Why this is not a method on the two services beside it

`FriendshipService` can end a friendship and `BlockingService` can place a
block. This can do neither, and it is what crosses the module boundary — the
narrowing `users.public` makes twelve times over. A consumer granted the
ability to read the graph must not thereby gain the ability to rewrite it,
and the module serving the platform's highest-volume public read is exactly
the consumer that must not.

It holds two **repositories** rather than the two services, for the same
reason: a service opens transactions, and nothing here writes.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from app.modules.friends.application.ports import (
    BlockedPlayerRepository,
    FriendshipRepository,
)

logger = logging.getLogger(__name__)


class SocialGraphReaderService:
    """Reads the social graph, and can do nothing else."""

    def __init__(
        self,
        *,
        friendships: FriendshipRepository,
        blocks: BlockedPlayerRepository,
    ) -> None:
        self._friendships = friendships
        self._blocks = blocks

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        """Which of `others` are currently friends with `player_id`.

        Straight delegation. The seam is the point rather than the code:
        `profiles` sees a port it can only read through, and this module
        keeps the freedom to change how the answer is computed — a cache
        under `friends:v1:` lands here without `profiles` learning it
        happened.
        """
        return await self._friendships.friend_ids_among(player_id, others)

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """Every player `player_id` cannot interact with, either direction.

        Also straight delegation, and the seam A64-013.6 filled: this is
        the read that runs on **every** profile composition and every
        search, and `CachedSocialGraphReader` now decorates this class to
        serve it from `friends:v1:`. The invalidation triggers caching.md
        C-1 demanded are `BlockingService.block` and `.unblock`, the only
        two writers of the relation beneath it.
        """
        return await self._blocks.blocked_ids_for(player_id)

    async def friend_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """Every live friend of this player — A64-013.6.

        Not on the published port: its only consumer is
        `CachedSocialGraphReader`, which needs the whole set to populate one
        cache entry. Publishing it would offer other modules a read whose
        cost grows with a player's friend count, for no use case any of them
        has.
        """
        return await self._friendships.friend_ids_for(player_id)
