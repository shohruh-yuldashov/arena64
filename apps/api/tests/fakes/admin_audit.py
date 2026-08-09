"""An in-memory `admin.audit_entry` — A64-024.8.

What is faked is **storage**, never the thing under test. `AuditRecorder`,
`AuditLog` and `AdminRoleService` all run for real against this.

## What it models, and what it deliberately does not

It models the two properties the callers' correctness rests on: entries
accumulate in the order they were appended, and a page is newest-first and
bounded with a cursor that continues it.

It does **not** model the append-only guarantee. That is a PostgreSQL
trigger, and a fake that refused an `update` it never offers would be a fake
agreeing with itself — the guarantee is asserted against a real database in
`tests/contract/test_admin_audit_entry.py`, which is the only place it can
be.
"""

from collections.abc import Sequence

from app.modules.admin.application.ports import AuditEntryFilters, AuditEntryPage
from app.modules.admin.domain.audit import AuditEntry, AuditSubjectType


class InMemoryAuditEntries:
    """The audit trail, as a list."""

    def __init__(self) -> None:
        self.rows: list[AuditEntry] = []

    async def append(self, entry: AuditEntry) -> AuditEntry:
        self.rows.append(entry)
        return entry

    async def page(
        self, *, filters: AuditEntryFilters, limit: int, cursor: str | None
    ) -> AuditEntryPage:
        rows = _newest_first(self.rows)

        if filters.action is not None:
            rows = [row for row in rows if row.action == filters.action]
        if filters.actor_id is not None:
            rows = [row for row in rows if row.actor_id == filters.actor_id]
        if filters.subject_type is not None:
            rows = [row for row in rows if row.subject_type == filters.subject_type]
            if filters.subject_ref is not None:
                rows = [row for row in rows if row.subject_ref == filters.subject_ref]

        if cursor is not None:
            # The position is the entry's id, which is enough for a list and
            # keeps the fake from re-implementing the real base64 keyset —
            # the ordering is what the caller depends on, not the encoding.
            after = [index for index, row in enumerate(rows) if str(row.id) == cursor]
            rows = rows[after[0] + 1 :] if after else []

        page = rows[:limit]
        has_more = len(rows) > limit
        return AuditEntryPage(
            entries=page,
            next_cursor=str(page[-1].id) if has_more and page else None,
        )

    def subjects_of(self, subject_type: AuditSubjectType) -> Sequence[str]:
        """Every subject reference of one kind, for a test that asserts on
        what was recorded rather than on how it paginates."""
        return [row.subject_ref for row in self.rows if row.subject_type is subject_type]


def _newest_first(rows: Sequence[AuditEntry]) -> list[AuditEntry]:
    """`(created_at, id)` descending — the real repository's ordering.

    The `id` tiebreak is modelled because it is the whole reason the keyset
    is total: entries written in the same millisecond are ordinary here, and
    a fake ordered by time alone would hide a caller that depends on a
    stable order.
    """
    return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)


__all__ = ["InMemoryAuditEntries"]
