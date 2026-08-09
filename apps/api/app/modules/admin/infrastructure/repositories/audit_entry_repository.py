"""`AuditEntryRepository` over SQLAlchemy — repositories.md §3.

Maps rows to `AuditEntry` values and back. Two methods, because the port has
two: there is no `update`, no `delete` and no `get`, and the database keeps
its own copy of that rule in a trigger — see `AuditEntryModel`.

## The keyset, and why it is `(created_at, id)`

Two grants made in the same millisecond are ordinary — an operator revoking
one role and granting another is exactly that — so `created_at` alone is not
unique and a cursor on it silently skips or repeats entries. The `id`
tiebreak makes the ordering total, and `ix_audit_entry__created_at_id`
exists so the seek is an index scan rather than a sort of the whole table.

`OFFSET` is not used and could not be: the trail grows at the head, so page
four of a `LIMIT/OFFSET` listing shows different rows depending on what was
written since page three was fetched.

One page is **one query**. There is no count — see `AuditEntryPage`.
"""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.modules.admin.application.ports import AuditEntryFilters, AuditEntryPage
from app.modules.admin.domain.audit import AuditEntry
from app.modules.admin.infrastructure.models import AuditEntryModel


class SqlAlchemyAuditEntryRepository:
    """The audit trail, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, entry: AuditEntry) -> AuditEntry:
        self._session.add(
            AuditEntryModel(
                id=entry.id,
                actor_type=entry.actor_type,
                actor_id=entry.actor_id,
                action=entry.action,
                subject_type=entry.subject_type,
                subject_ref=entry.subject_ref,
                outcome=entry.outcome,
                before=entry.before,
                after=entry.after,
                correlation_id=entry.correlation_id,
                created_at=entry.created_at,
            )
        )
        # Flushed, never committed: the transaction belongs to the unit of
        # work the *caller* opened around its own mutation
        # (repositories.md §5.1). Committing here would let an action roll
        # back while its entry survived, which is the failure mode
        # `AuditRecorder` exists to prevent.
        await self._session.flush()
        return entry

    async def page(
        self, *, filters: AuditEntryFilters, limit: int, cursor: str | None
    ) -> AuditEntryPage:
        statement = select(AuditEntryModel)

        if filters.action is not None:
            statement = statement.where(AuditEntryModel.action == filters.action)
        if filters.actor_id is not None:
            statement = statement.where(AuditEntryModel.actor_id == filters.actor_id)
        if filters.subject_type is not None:
            statement = statement.where(AuditEntryModel.subject_type == filters.subject_type)
            # Only ever alongside its type — `ix_audit_entry__subject` leads
            # with `subject_type`, and a bare ref would skip that column.
            if filters.subject_ref is not None:
                statement = statement.where(AuditEntryModel.subject_ref == filters.subject_ref)

        if cursor is not None:
            after = _AuditCursor.decode(cursor)
            statement = statement.where(
                # A row-value comparison, so the keyset is one index seek
                # rather than the `(a < x) OR (a = x AND b < y)` expansion a
                # planner cannot always fold back into one.
                tuple_(AuditEntryModel.created_at, AuditEntryModel.id)
                < tuple_(literal(after.created_at), literal(after.entry_id))
            )

        # Over-fetch by one to learn whether a further page exists, rather
        # than a `COUNT(*)` that scans a table designed never to shrink.
        rows = (
            (
                await self._session.execute(
                    statement.order_by(
                        AuditEntryModel.created_at.desc(), AuditEntryModel.id.desc()
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )

        has_more = len(rows) > limit
        page = list(rows[:limit])
        next_cursor = (
            _AuditCursor(created_at=page[-1].created_at, entry_id=page[-1].id).encode()
            if has_more and page
            else None
        )
        return AuditEntryPage(entries=[_to_domain(row) for row in page], next_cursor=next_cursor)


def _to_domain(row: AuditEntryModel) -> AuditEntry:
    return AuditEntry(
        id=row.id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        subject_type=row.subject_type,
        subject_ref=row.subject_ref,
        outcome=row.outcome,
        created_at=row.created_at,
        correlation_id=row.correlation_id,
        before=dict(row.before or {}),
        after=dict(row.after or {}),
    )


@dataclass(frozen=True, slots=True)
class _AuditCursor:
    """The keyset position, as an opaque string.

    Base64 of `created_at|id`, which is not security — a caller may decode
    it — but is what stops a client treating it as an offset it may
    arithmetic on. An unparseable cursor raises rather than silently
    starting from the beginning, because "page 4 quietly became page 1" is
    the bug nobody reports.
    """

    created_at: datetime
    entry_id: UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.entry_id}"
        return urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str) -> "_AuditCursor":
        padding = "=" * (-len(cursor) % 4)
        try:
            raw = urlsafe_b64decode(cursor + padding).decode()
            moment, identifier = raw.split("|", 1)
            return cls(created_at=datetime.fromisoformat(moment), entry_id=UUID(identifier))
        except (ValueError, TypeError) as exc:
            raise ValidationError("That page cursor could not be read.") from exc


__all__ = ["SqlAlchemyAuditEntryRepository"]
