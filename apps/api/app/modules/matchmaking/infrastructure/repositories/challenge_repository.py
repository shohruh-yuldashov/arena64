"""The SQLAlchemy adapter for `application.ports.ChallengeRepository`.

Database-only, per repositories.md §2. It decides *how* a challenge is stored
and read, never *whether* one may be — that is `ChallengeService`'s, and it
is settled before anything here runs.

## The insert may be refused, and that is the design

`uq_friend_challenge__live_pair` is a partial unique index over the
**unordered** pair, so two people cannot hold two live invitations between
them — not two in the same direction and not one each way. `add` therefore
translates the integrity error into `ConflictError` rather than checking
first: a check-then-insert loses the race between two simultaneous creates,
and the pair that races is precisely the one the rule is about (two friends
challenging each other at the same moment).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import ConflictError
from app.modules.matchmaking.domain.challenge import Challenge, ChallengeStatus
from app.modules.matchmaking.infrastructure.challenge_cursor import ChallengeCursor
from app.modules.matchmaking.infrastructure.models import FriendChallengeModel


class SqlAlchemyChallengeRepository:
    """Constructed per unit of work with the active session
    (repositories.md §5.1) — never holds one longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, challenge: Challenge) -> None:
        """Inserts a pending challenge, or refuses the pair's second one.

        `flush` rather than waiting for the commit, so the conflict surfaces
        here — where it can be translated — instead of at the end of the unit
        of work, where the caller has no idea which statement caused it.

        The message deliberately names neither player and neither direction:
        it is the same sentence whether the existing challenge was sent by
        this caller or to them, because telling them which would say whether
        the other person has challenged them, which they may not have been
        shown yet.
        """
        self._session.add(_to_model(challenge))
        try:
            await self._session.flush()
        except IntegrityError as conflict:
            raise ConflictError(
                "There is already a live challenge between these players.",
                code=ErrorCode.CHALLENGE_ALREADY_PENDING,
            ) from conflict

    async def get_for_party(
        self, challenge_id: uuid.UUID, *, party_id: uuid.UUID
    ) -> Challenge | None:
        """One challenge, scoped to somebody who is part of it.

        The party is half the predicate rather than a filter applied
        afterwards, so there is no code path that reads a row and then
        decides whether the caller was entitled to it.
        """
        row = await self._session.scalar(
            select(FriendChallengeModel).where(
                FriendChallengeModel.id == challenge_id,
                or_(
                    FriendChallengeModel.challenger_id == party_id,
                    FriendChallengeModel.recipient_id == party_id,
                ),
            )
        )
        return None if row is None else _to_domain(row)

    async def save(self, challenge: Challenge) -> None:
        """Writes a settled challenge back.

        **Guarded on `status = 'pending'`**, which is the concurrency rule
        rather than an optimisation: two people acting at the same instant —
        the recipient declining while the challenger cancels — both hold a
        `PENDING` aggregate and both produce a valid terminal one. The
        predicate means the second `UPDATE` matches no row, and the caller is
        told the challenge was already answered instead of overwriting the
        first outcome.

        Only the three columns a transition can move. The settings and the
        parties are immutable by product rule (§2: no editing a sent
        challenge), so an `UPDATE` that could set them would be a way to
        change what somebody agreed to after they agreed.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(FriendChallengeModel)
                .where(
                    FriendChallengeModel.id == challenge.id,
                    FriendChallengeModel.status == ChallengeStatus.PENDING.value,
                )
                .values(
                    status=challenge.status.value,
                    responded_at=challenge.responded_at,
                    created_match_id=challenge.created_match_id,
                )
            ),
        )
        if not result.rowcount:
            raise ConflictError("This challenge has already been answered.")

    async def list_for_party(
        self,
        party_id: uuid.UUID,
        *,
        as_challenger: bool,
        now: datetime,
        limit: int,
        cursor: str | None,
    ) -> tuple[Sequence[Challenge], str | None]:
        """Keyset page over `(created_at DESC, id DESC)`.

        **Newest first**, which is what a challenge list means: the thing you
        have not answered yet is the thing you just received. That makes the
        keyset a *descending* one, so the cursor predicate is `<` and runs
        the same direction as the `ORDER BY` — getting one of those backwards
        silently returns an empty second page, which is why both are written
        here rather than assembled by a caller.

        `id` is the unique tiebreak. `created_at` alone is not unique — two
        challenges can share a millisecond — and a keyset without a unique
        tiebreak skips or repeats rows at a page boundary.

        **`expires_at > now` is in the predicate**, not applied afterwards.
        Filtering a fetched page would make `limit` mean "up to twenty, fewer
        if some expired", so a page could come back empty with a cursor still
        pointing at live rows further down.

        Over-fetches by one to learn whether a further page exists without a
        second count (RP-03).
        """
        party = (
            FriendChallengeModel.challenger_id == party_id
            if as_challenger
            else FriendChallengeModel.recipient_id == party_id
        )
        statement = select(FriendChallengeModel).where(
            party,
            FriendChallengeModel.status == ChallengeStatus.PENDING.value,
            FriendChallengeModel.expires_at > now,
        )

        if cursor is not None:
            position = ChallengeCursor.decode(cursor)
            statement = statement.where(
                or_(
                    FriendChallengeModel.created_at < position.created_at,
                    and_(
                        FriendChallengeModel.created_at == position.created_at,
                        FriendChallengeModel.id < position.row_id,
                    ),
                )
            )

        statement = statement.order_by(
            FriendChallengeModel.created_at.desc(), FriendChallengeModel.id.desc()
        ).limit(limit + 1)

        rows = list((await self._session.scalars(statement)).all())
        page = rows[:limit]

        next_cursor: str | None = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = ChallengeCursor(created_at=last.created_at, row_id=last.id).encode()

        return [_to_domain(row) for row in page], next_cursor

    async def find_live_between(self, first: uuid.UUID, second: uuid.UUID) -> Challenge | None:
        """The pending challenge between these two, whichever direction.

        Ordered comparison against the same expressions the unique index is
        built on (`least`/`greatest`), so the query the service asks and the
        constraint the database enforces are the same question — and the
        index answers it.
        """
        low, high = sorted((first, second), key=str)
        row = await self._session.scalar(
            select(FriendChallengeModel).where(
                func.least(FriendChallengeModel.challenger_id, FriendChallengeModel.recipient_id)
                == low,
                func.greatest(FriendChallengeModel.challenger_id, FriendChallengeModel.recipient_id)
                == high,
                FriendChallengeModel.status == ChallengeStatus.PENDING.value,
            )
        )
        return None if row is None else _to_domain(row)


def _to_model(challenge: Challenge) -> FriendChallengeModel:
    return FriendChallengeModel(
        id=challenge.id,
        challenger_id=challenge.challenger_id,
        recipient_id=challenge.recipient_id,
        time_control_id=challenge.time_control_id,
        variant=challenge.variant,
        rated=challenge.rated,
        status=challenge.status,
        created_at=challenge.created_at,
        expires_at=challenge.expires_at,
        responded_at=challenge.responded_at,
        created_match_id=challenge.created_match_id,
    )


def _to_domain(row: FriendChallengeModel) -> Challenge:
    return Challenge(
        id=row.id,
        challenger_id=row.challenger_id,
        recipient_id=row.recipient_id,
        time_control_id=row.time_control_id,
        variant=row.variant,
        rated=row.rated,
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        responded_at=row.responded_at,
        created_match_id=row.created_match_id,
    )


__all__ = ["SqlAlchemyChallengeRepository"]
