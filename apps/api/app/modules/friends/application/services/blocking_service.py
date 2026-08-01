"""`BlockingService` — place a block, lift one, list them.

Orchestrates; does not compute (services.md §3.2). The self-block rule is
the aggregate's, uniqueness is the index's, and the *cascade* is this
service's — because it is the one thing that spans three relations and
therefore belongs to nothing smaller.

## Blocking is one transaction across three relations

BL-2: "a block suppresses friend requests, direct challenges, direct
messages, presence visibility, and **matchmaking pairing**." Two of those
are state that already exists when the block is placed, and both must
change with it:

    1. the block is written
    2. any live friendship ends, with `FriendshipEndReason.BLOCKED` (FS-3)
    3. any pending request between the two is voided (FR-2)

All three in one unit of work, committed once. The failure a split would
permit is not self-correcting and nothing would ever notice it: a block that
suppressed *future* contact while leaving the friendship live means the two
still appear in each other's friend lists, still see each other's
friends-only fields, and — once A64-013.6 lands — still see each other's
presence. That is the block silently not working, for the one person who
asked for it.

The same argument FR-4 makes about acceptance, applied to a wider write.

## Why blocking is not idempotent and unblocking is

`block` raises `AlreadyBlocked` on a repeat; `unblock` succeeds on one.

The asymmetry follows from side effects. Blocking runs a cascade, and a
second block would find the friendship already ended and the requests
already voided — reporting success would tell the caller a cascade ran that
did not, which matters because a client showing "blocked and 1 friendship
ended" would be lying. Unblocking has no cascade: it deletes a row, and
deleting a row that is already gone reaches the state the caller wanted.

`DELETE` is idempotent by HTTP semantics too, which the endpoint honours.

## What blocking deliberately does *not* do

**It does not restore anything on unblock.** A friendship ended by a block
stays ended, and the two must send a fresh request. BL-3 — "blocks do not
rewrite history" — cuts both ways: the block did not erase the friendship,
and lifting it does not resurrect one. Reinstating would also mean deciding
what to do about a friendship the *other* party ended in the meantime,
which is a question with no correct answer.

**It does not notify.** BL-1: the blocked player is never told. Nothing here
emits an event, and A64-013.6's presence integration must not either.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.friends.application.ports import (
    BlockedPlayerRepository,
    FriendRequestRepository,
    FriendshipRepository,
)
from app.modules.friends.domain.block import Block
from app.modules.friends.domain.exceptions import AlreadyBlocked, NotBlocked, SelfBlock
from app.modules.friends.domain.friendship import FriendshipEndReason

logger = logging.getLogger(__name__)


class BlockingService:
    def __init__(
        self,
        *,
        blocks: BlockedPlayerRepository,
        friendships: FriendshipRepository,
        requests: FriendRequestRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._blocks = blocks
        # The two **repositories**, not the services that wrap them. Those
        # open transactions of their own, and calling them from inside the
        # cascade's unit of work would produce the nested, multi-transaction
        # shape this service exists to avoid. What the cascade needs are
        # writes that join its transaction, which is what a repository is.
        self._friendships = friendships
        self._requests = requests
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def block(self, *, blocker_id: UUID, blocked_id: UUID) -> Block:
        """Blocks `blocked_id` on behalf of `blocker_id`, and runs the
        cascade.

        Raises `SelfBlock` (422) and `AlreadyBlocked` (409). The duplicate
        check runs before any I/O beyond one indexed read, and the unique
        index is what actually enforces it under concurrency (BE-06) — which
        matters here more than elsewhere, because a second block slipping
        through would re-end an already-ended friendship.

        Everything below the first `await` happens in **one transaction**;
        see this module's docstring for what a split would permit.
        """
        if blocker_id == blocked_id:
            # Checked before the aggregate constructs, so the caller gets
            # the error without a round trip. `Block.__post_init__` and
            # `ck_blocked_player__not_self` are the other two copies.
            raise SelfBlock("A player cannot block themselves.")

        if await self._blocks.exists(blocker_id, blocked_id):
            raise AlreadyBlocked("You have already blocked that player.")

        at = self._clock.now()
        block = Block.place(blocker_id=blocker_id, blocked_id=blocked_id, at=at)

        async with self._unit_of_work:
            stored = await self._blocks.add(block)
            ended = await self._end_friendship(blocker_id, blocked_id, at=at)
            voided = await self._requests.void_pending_between(blocker_id, blocked_id, at=at)
            await self._unit_of_work.commit()

        # **Both ids, and the counts of what the cascade did.** A block is
        # an edge one party created and the other must never learn about, so
        # this line is more sensitive than most — but it is also the record
        # an operator needs when a player reports that blocking "did not
        # work", and it names no username, no display name and no reason
        # (services.md §8.5).
        logger.info(
            "player_blocked",
            extra={
                "blocker_id": str(blocker_id),
                "blocked_id": str(blocked_id),
                "friendship_ended": ended,
                "requests_voided": voided,
            },
        )
        return stored

    async def unblock(self, *, blocker_id: UUID, blocked_id: UUID) -> None:
        """Lifts a block. **Idempotent** — lifting one that is not there
        succeeds and changes nothing.

        Two reasons, the second of which is the one that matters:

          - `DELETE` is idempotent by HTTP semantics, so a client retrying
            after a dropped response must not be told the resource is gone
            when its own first attempt removed it.
          - It keeps the endpoint from answering a question it should not.
            A `404` for "not blocked" beside a `204` for "was blocked" lets
            anybody probe their own block list state — harmless on its own,
            and the same reasoning that made `DELETE /friends/{id}`
            idempotent in A64-013.4.

        **Restores nothing.** A friendship the block ended stays ended and a
        request it voided stays voided; the two must start again. BL-3 cuts
        both ways.
        """
        try:
            async with self._unit_of_work:
                await self._blocks.remove(blocker_id, blocked_id)
                await self._unit_of_work.commit()
        except NotBlocked:
            # Nothing to lift. DEBUG rather than INFO: a retry is ordinary,
            # and an audit trail records what *changed*.
            logger.debug("player_unblock_noop", extra={"blocker_id": str(blocker_id)})
            return

        logger.info(
            "player_unblocked",
            extra={"blocker_id": str(blocker_id), "blocked_id": str(blocked_id)},
        )

    async def list_blocked(
        self, *, blocker_id: UUID, limit: int, cursor: str | None
    ) -> tuple[Sequence[Block], str | None]:
        """The blocks this player has placed, newest first.

        Read-only; opens no transaction. Scoped to the caller by
        construction — there is no parameter that could name another
        player's block list, which is why this needs no ownership check.

        Returns the aggregates rather than composed profiles: turning blocks
        into people is the presentation layer's job, through
        `ProfileDirectoryService`.
        """
        return await self._blocks.list_for_blocker(blocker_id, limit=limit, cursor=cursor)

    async def _end_friendship(self, blocker_id: UUID, blocked_id: UUID, *, at: datetime) -> bool:
        """Ends any live friendship between the two. Returns whether there
        was one.

        FS-3: "a block immediately voids any friendship — blocking must not
        require a second action to be effective."

        `FriendshipEndReason.BLOCKED` rather than `REMOVED`, and the
        distinction is load-bearing rather than descriptive: it is what
        `_ensure_not_blocked` and any future re-friending rule read, and
        what tells an operator why a friendship in the history ended.

        Ends it **as the blocker**, which passes `Friendship.end`'s
        participation check — the blocker is by definition one of the two,
        since a friendship existed between them.
        """
        friendship = await self._friendships.friendship_by_players(blocker_id, blocked_id)
        if friendship is None:
            return False

        friendship.end(by=blocker_id, at=at, reason=FriendshipEndReason.BLOCKED)
        await self._friendships.remove(friendship)

        # Separate from `player_blocked` because it answers a different
        # question — "why did this friendship end" — and because a
        # friendship ending is a fact both parties experience, unlike the
        # block itself.
        logger.info(
            "friendship_terminated_by_block",
            extra={"friendship_id": str(friendship.id), "actor_id": str(blocker_id)},
        )
        return True
