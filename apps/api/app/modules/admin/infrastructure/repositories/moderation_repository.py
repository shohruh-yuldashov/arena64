"""Moderation storage over SQLAlchemy — repositories.md §3. A64-024.6.

Two adapters, mirroring the two tables DM-12 keeps apart, plus the one
published read `auth` programs against.

The sanction adapter satisfies **both** `admin.application.ports.
SanctionRepository` (what the console needs) and `admin.public.
AccountRestrictionGate` (what `auth` needs) — one object, two interfaces,
because they are two questions about the same rows and a second adapter
would be a second place for the effectiveness rule to live.
"""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.modules.admin.application.ports import SanctionPage
from app.modules.admin.domain.moderation import (
    ModerationCase,
    Sanction,
    SanctionKind,
)
from app.modules.admin.infrastructure.models import ModerationCaseModel, SanctionModel
from app.modules.admin.public.moderation import AccountRestriction


class SqlAlchemyModerationCaseRepository:
    """Decision records, in PostgreSQL. **No update method exists** — §13.2."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, case: ModerationCase) -> ModerationCase:
        self._session.add(
            ModerationCaseModel(
                id=case.id,
                subject_player_id=case.subject_player_id,
                category=case.category,
                status=case.status,
                opened_by=case.opened_by,
                opened_at=case.opened_at,
                closed_at=case.closed_at,
                decision=case.decision,
                reasoning=case.reasoning,
                reverses_case_id=None,
            )
        )
        # Flushed rather than committed: the unit of work owns the
        # transaction boundary (repositories.md §5.1), and flushing here is
        # what makes the sanction's foreign key resolvable in the same
        # statement batch.
        await self._session.flush()
        return case

    async def cases_by_ids(self, case_ids: Sequence[UUID]) -> Mapping[UUID, ModerationCase]:
        if not case_ids:
            return {}
        rows = (
            (
                await self._session.execute(
                    select(ModerationCaseModel).where(ModerationCaseModel.id.in_(set(case_ids)))
                )
            )
            .scalars()
            .all()
        )
        return {row.id: _case_to_domain(row) for row in rows}


class SqlAlchemySanctionRepository:
    """Enforced restrictions, in PostgreSQL.

    Also `admin.public.AccountRestrictionGate` — see the module docstring.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, sanction: Sanction) -> Sanction:
        self._session.add(
            SanctionModel(
                id=sanction.id,
                player_id=sanction.player_id,
                case_id=sanction.case_id,
                kind=sanction.kind,
                starts_at=sanction.starts_at,
                expires_at=sanction.expires_at,
                lifted_at=sanction.lifted_at,
                lifted_by=sanction.lifted_by,
                created_at=sanction.created_at,
            )
        )
        # Surfaces `uq_sanction__live_kind` here rather than at commit, so
        # a race between two administrators is an error the service can
        # attribute instead of one that appears after the audit entry.
        await self._session.flush()
        return sanction

    async def lift(self, sanction: Sanction) -> Sanction:
        row = await self._session.get(SanctionModel, sanction.id)
        if row is None:  # pragma: no cover — the service read it moments ago
            return sanction
        row.lifted_at = sanction.lifted_at
        row.lifted_by = sanction.lifted_by
        await self._session.flush()
        return sanction

    async def effective_for(self, player_id: UUID, *, at: datetime) -> Sequence[Sanction]:
        """Q6 — the hot authorization read.

        `lifted_at IS NULL` matches `ix_sanction__player_expiry`'s partial
        predicate exactly, and the expiry comparison is expressed in SQL
        rather than in Python so the index's second column narrows the scan
        instead of every unlifted row travelling to the application.
        """
        rows = (
            (
                await self._session.execute(
                    select(SanctionModel).where(
                        SanctionModel.player_id == player_id,
                        SanctionModel.lifted_at.is_(None),
                        SanctionModel.starts_at <= at,
                        (SanctionModel.expires_at.is_(None)) | (SanctionModel.expires_at > at),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_sanction_to_domain(row) for row in rows]

    async def live_of_kind(self, player_id: UUID, kind: SanctionKind) -> Sanction | None:
        row = await self._session.scalar(
            select(SanctionModel).where(
                SanctionModel.player_id == player_id,
                SanctionModel.kind == kind,
                SanctionModel.lifted_at.is_(None),
            )
        )
        return None if row is None else _sanction_to_domain(row)

    async def page(
        self, *, effective_at: datetime | None, limit: int, cursor: str | None
    ) -> SanctionPage:
        statement = select(SanctionModel)

        if effective_at is not None:
            statement = statement.where(
                SanctionModel.lifted_at.is_(None),
                SanctionModel.starts_at <= effective_at,
                (SanctionModel.expires_at.is_(None)) | (SanctionModel.expires_at > effective_at),
            )

        if cursor is not None:
            after = _SanctionCursor.decode(cursor)
            statement = statement.where(
                # A row-value comparison, so the keyset is one index seek
                # rather than an expansion the planner may not fold back.
                tuple_(SanctionModel.created_at, SanctionModel.id)
                < tuple_(literal(after.created_at), literal(after.sanction_id))
            )

        # Over-fetch by one instead of a `COUNT(*)`, which on a table that
        # only accumulates would get slower every month.
        rows = (
            (
                await self._session.execute(
                    statement.order_by(
                        SanctionModel.created_at.desc(), SanctionModel.id.desc()
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )

        has_more = len(rows) > limit
        page = list(rows[:limit])
        next_cursor = (
            _SanctionCursor(created_at=page[-1].created_at, sanction_id=page[-1].id).encode()
            if has_more and page
            else None
        )
        return SanctionPage(
            sanctions=[_sanction_to_domain(row) for row in page], next_cursor=next_cursor
        )

    # --- `admin.public.AccountRestrictionGate` -------------------------------

    async def restriction_for(self, player_id: UUID, *, at: datetime) -> AccountRestriction | None:
        """What `auth` asks at every credential boundary.

        Returns the **latest** expiry among the restrictions in force, so
        an account carrying an indefinite restriction alongside a timed one
        is reported as indefinite — §13.3's "overlapping sanctions apply
        the most restrictive", applied to the only dimension this DTO has.
        """
        effective = await self.effective_for(player_id, at=at)
        if not effective:
            return None
        if any(sanction.expires_at is None for sanction in effective):
            return AccountRestriction(until=None)
        return AccountRestriction(
            until=max(
                sanction.expires_at for sanction in effective if sanction.expires_at is not None
            )
        )


def _case_to_domain(row: ModerationCaseModel) -> ModerationCase:
    return ModerationCase(
        id=row.id,
        subject_player_id=row.subject_player_id,
        category=row.category,
        status=row.status,
        opened_by=row.opened_by,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        decision=row.decision,
        reasoning=row.reasoning,
    )


def _sanction_to_domain(row: SanctionModel) -> Sanction:
    return Sanction(
        id=row.id,
        player_id=row.player_id,
        case_id=row.case_id,
        kind=row.kind,
        starts_at=row.starts_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
        lifted_at=row.lifted_at,
        lifted_by=row.lifted_by,
    )


@dataclass(frozen=True, slots=True)
class _SanctionCursor:
    """The keyset position, as an opaque string — the same shape the users,
    matches, tournaments and audit listings use."""

    created_at: datetime
    sanction_id: UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.sanction_id}"
        return urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str) -> "_SanctionCursor":
        padding = "=" * (-len(cursor) % 4)
        try:
            raw = urlsafe_b64decode(cursor + padding).decode()
            moment, identifier = raw.split("|", 1)
            return cls(created_at=datetime.fromisoformat(moment), sanction_id=UUID(identifier))
        except (ValueError, TypeError) as exc:
            raise ValidationError("That page cursor could not be read.") from exc


__all__ = ["SqlAlchemyModerationCaseRepository", "SqlAlchemySanctionRepository"]
