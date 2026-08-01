"""The SQLAlchemy adapter for `application.ports.BlockedPlayerRepository`.

Database-only (repositories.md §2): decides *how* to store and fetch, never
*whether*. Every "is this allowed" question is answered by `Block` and
`BlockingService`.

## Two directional reads, one symmetric answer

`blocked_ids_for` returns both "who I blocked" and "who blocked me" in a
single set, because that is what every consumer needs: the visibility
consequence of a block runs both ways even though the fact is
one-directional (see `ViewerRelationship.BLOCKED`). Splitting it would push
the union into three call sites — the relationship provider, the search
exclusion, and the request validator — and the one that forgot a direction
would leak in exactly the way BL-1 forbids.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.friends.domain.block import Block
from app.modules.friends.domain.exceptions import AlreadyBlocked, NotBlocked
from app.modules.friends.infrastructure.list_cursor import ListCursor
from app.modules.friends.infrastructure.models import BlockedPlayerModel

logger = logging.getLogger(__name__)

#: The one constraint this adapter translates. Keyed by the name declared in
#: `models.py.__table_args__` — renaming it there without renaming it here
#: silently stops the translation working, which is why a contract test
#: drives a real violation through this path.
_UNIQUE_PAIR_INDEX = "uq_blocked_player__pair"


class SqlAlchemyBlockedPlayerRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1) — never holds a session longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: BlockedPlayerModel) -> Block:
        return Block(
            id=row.id,
            blocker_id=row.blocker_id,
            blocked_id=row.blocked_id,
            created_at=row.created_at,
        )

    async def add(self, block: Block) -> Block:
        """Persists a new block.

        Raises `AlreadyBlocked` when the unique index refuses a duplicate.
        The service checks first to produce a good error cheaply; this is
        the guard that holds under concurrency (BE-06), and it matters more
        here than usual — a second block that slipped through would run the
        cascade twice, re-ending an already-ended friendship.

        **Flushes, never commits.** The caller's unit of work spans the
        block, the friendship it ends and the requests it voids: one
        transaction, because a block that suppressed contact without
        terminating the friendship would be a state nothing reconciles.
        """
        row = BlockedPlayerModel(
            id=block.id,
            blocker_id=block.blocker_id,
            blocked_id=block.blocked_id,
            created_at=block.created_at,
        )
        self._session.add(row)

        try:
            await self._session.flush()
        except IntegrityError as error:
            raise self._translate(error) from error

        return self._to_domain(row)

    async def exists(self, blocker_id: UUID, blocked_id: UUID) -> bool:
        """Whether `blocker_id` has blocked `blocked_id`.

        **Directional**, unlike `blocked_ids_for` below. This answers "did
        *this* player place a block", which is what unblocking and duplicate
        detection need; the symmetric question is a different one.
        """
        found = await self._session.scalar(
            select(BlockedPlayerModel.id)
            .where(
                BlockedPlayerModel.blocker_id == blocker_id,
                BlockedPlayerModel.blocked_id == blocked_id,
            )
            .limit(1)
        )
        return found is not None

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """Every player this one cannot interact with, in **either**
        direction.

        The set behind `ViewerRelationship.BLOCKED`, the search exclusion
        and the friend-request refusal. One query with an `OR` over the two
        directional indexes, which PostgreSQL `BitmapOr`s.

        Returns the *other* players' ids, so a caller never has to work out
        which side of each row it was on.

        **Unbounded today, and bounded by design later.** BL-4 makes block
        capacity a product decision precisely because an unbounded block
        list interacts badly with BL-2's matchmaking filter. Until that
        number exists this loads the whole set, which is correct and is fine
        at any plausible per-player count; the note is here so the person
        who sets the bound knows this is the read that motivated it.

        A `frozenset` because every consumer only tests membership, and
        because it is handed to `UserSearchQuery.exclude_player_ids`, which
        is frozen.
        """
        rows = await self._session.execute(
            select(BlockedPlayerModel.blocker_id, BlockedPlayerModel.blocked_id).where(
                or_(
                    BlockedPlayerModel.blocker_id == player_id,
                    BlockedPlayerModel.blocked_id == player_id,
                )
            )
        )
        return frozenset(blocked if blocker == player_id else blocker for blocker, blocked in rows)

    async def blocked_pairs_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        """Which of `player_ids` may not be paired with which — A64-015.3.

        **One query for the whole batch**, which is the entire reason this
        exists beside `blocked_ids_for`: the pairing scan holds up to a few
        hundred candidates, and asking the symmetric question per candidate
        would be that many round trips inside a job that runs continuously.

        Both sides of the predicate are restricted to the batch, so the
        index this rides — `ix_blocked_player__blocker` — is probed once per
        blocker and the result set is bounded by the blocks *within* the
        pool rather than by either player's whole block list. A block
        against somebody who is not queueing is irrelevant here and never
        leaves the database.

        The mapping is built symmetric because BL-2's consequence is:
        whichever direction the row runs, the pair is unpairable, and a
        caller checking only one direction would pair exactly the halves
        that a one-directional read missed.

        Players with no exclusions are absent from the mapping rather than
        present with an empty set — the common case is that nobody in a pool
        has blocked anybody, and that case should allocate nothing.
        """
        if len(player_ids) < 2:
            return {}

        unique = list(set(player_ids))
        rows = await self._session.execute(
            select(BlockedPlayerModel.blocker_id, BlockedPlayerModel.blocked_id).where(
                BlockedPlayerModel.blocker_id.in_(unique),
                BlockedPlayerModel.blocked_id.in_(unique),
            )
        )

        pairs: dict[UUID, set[UUID]] = {}
        for blocker, blocked in rows:
            pairs.setdefault(blocker, set()).add(blocked)
            pairs.setdefault(blocked, set()).add(blocker)
        return {player_id: frozenset(others) for player_id, others in pairs.items()}

    async def list_for_blocker(
        self, blocker_id: UUID, *, limit: int, cursor: str | None
    ) -> tuple[Sequence[Block], str | None]:
        """The blocks this player has **placed**, newest first,
        keyset-paginated.

        Deliberately one-directional, unlike `blocked_ids_for`. A block list
        is a management surface: it shows what you can lift, and blocks
        placed *on* you are not yours to see — BL-1 keeps them invisible,
        which is the whole reason a block is worth placing.

        Over-fetches by one to learn whether a further page exists without a
        second count (RP-03).
        """
        statement = select(BlockedPlayerModel).where(BlockedPlayerModel.blocker_id == blocker_id)

        if cursor is not None:
            position = ListCursor.decode(cursor)
            statement = statement.where(
                or_(
                    BlockedPlayerModel.created_at < position.created_at,
                    and_(
                        BlockedPlayerModel.created_at == position.created_at,
                        BlockedPlayerModel.id < position.row_id,
                    ),
                )
            )

        statement = statement.order_by(
            BlockedPlayerModel.created_at.desc(), BlockedPlayerModel.id.desc()
        ).limit(limit + 1)

        rows = list((await self._session.scalars(statement)).all())

        has_more = len(rows) > limit
        page = rows[:limit]

        next_cursor: str | None = None
        if has_more and page:
            last = page[-1]
            next_cursor = ListCursor(created_at=last.created_at, row_id=last.id).encode()

        return [self._to_domain(row) for row in page], next_cursor

    async def remove(self, blocker_id: UUID, blocked_id: UUID) -> None:
        """Lifts a block. **Hard delete** — database.md §7.2.

        The one relation on this platform that is genuinely deleted rather
        than soft-ended, and §7.2 gives the reason: "retaining released
        blocks would make BL-2's matchmaking filter — already the most
        performance-sensitive use of this relation — read rows it must then
        exclude."

        Raises `NotBlocked` when no row matched. `BlockingService.unblock`
        catches that and succeeds, because unblocking is idempotent; the
        exception exists so the repository reports what happened rather than
        deciding what it means.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(BlockedPlayerModel).where(
                    BlockedPlayerModel.blocker_id == blocker_id,
                    BlockedPlayerModel.blocked_id == blocked_id,
                )
            ),
        )

        if result.rowcount == 0:
            raise NotBlocked("No block to lift.")

    @staticmethod
    def _translate(error: IntegrityError) -> Exception:
        """A driver `IntegrityError` as the typed exception the service
        would have raised.

        Only the unique index is translated. `ck_blocked_player__not_self`
        is unreachable from the application — the aggregate refuses a
        self-block before construction — so a violation means a row was
        written by something other than this code path, and the honest
        outcome is the generic 500 an untranslated `IntegrityError` becomes.
        """
        constraint = getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)
        if constraint == _UNIQUE_PAIR_INDEX:
            return AlreadyBlocked("You have already blocked that player.")
        return error
