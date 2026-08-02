"""In-memory stand-ins for the two audit surfaces — A64-015.6 §3 and §4.

What is faked is **storage**, never the thing under test. `MatchOutcomeService`
and `ReconciliationTimelineProjector` run for real against these, so the
"a bar is always explained" pairing and the projection's idempotency are
genuinely exercised.

## Both model their unique index, and that is why they exist

`record` and `append` return the *existing* row on a duplicate rather than
writing a second one, which is what the real `ON CONFLICT DO NOTHING` plus
re-read produces. Modelled because both callers are outbox consumers under an
at-least-once contract, so the duplicate path is the one a redelivery takes —
and a fake that appended twice would leave the idempotency untested on the
path that enforces it.

What is **not** modelled is the atomicity: two concurrent writers against real
PostgreSQL resolve inside one statement, and here they would interleave. That
belongs to the database and is asserted in
`tests/contract/test_matchmaking_audit.py` with real sessions — the same line
`tests/fakes/queue_repository.py` draws about `SKIP LOCKED`.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from app.modules.matchmaking.domain.cooldown_audit import CooldownRecord
from app.modules.matchmaking.domain.reconciliation_timeline import ReconciliationEntry


class InMemoryCooldownAuditRepository:
    """The `queue_cooldown_audit` relation, as a list."""

    def __init__(self) -> None:
        self.records: list[CooldownRecord] = []

    async def record(self, entry: CooldownRecord) -> CooldownRecord:
        """Refuses a second row for one `(player_id, source_match_id)`, as
        `uq_queue_cooldown_audit__source` does — and returns the first, which
        is what the real adapter's re-read produces."""
        existing = next(
            (
                row
                for row in self.records
                if row.player_id == entry.player_id
                and row.source_match_id is not None
                and row.source_match_id == entry.source_match_id
            ),
            None,
        )
        if existing is not None:
            return existing

        self.records.append(entry)
        return entry

    async def history_for(self, player_id: UUID, *, limit: int) -> Sequence[CooldownRecord]:
        return sorted(
            (row for row in self.records if row.player_id == player_id),
            key=lambda row: row.applied_at,
            reverse=True,
        )[:limit]

    async def prune_recorded(self, *, before: datetime, batch_size: int) -> int:
        stale = sorted(
            (row for row in self.records if row.applied_at < before),
            key=lambda row: row.applied_at,
        )[:batch_size]
        for row in stale:
            self.records.remove(row)
        return len(stale)


class InMemoryReconciliationTimelineRepository:
    """The `pairing_timeline` relation, as a list."""

    def __init__(self) -> None:
        self.entries: list[ReconciliationEntry] = []
        self.fails = False

    async def append(self, entry: ReconciliationEntry) -> ReconciliationEntry:
        if self.fails:
            raise RuntimeError("the timeline relation is unreachable")

        existing = next((row for row in self.entries if row.event_id == entry.event_id), None)
        if existing is not None:
            return existing

        self.entries.append(entry)
        return entry

    async def for_ticket(self, ticket_id: UUID, *, limit: int) -> Sequence[ReconciliationEntry]:
        return sorted(
            (row for row in self.entries if row.ticket_id == ticket_id),
            key=lambda row: row.occurred_at,
            reverse=True,
        )[:limit]

    async def for_pairing(self, pairing_id: UUID, *, limit: int) -> Sequence[ReconciliationEntry]:
        return sorted(
            (row for row in self.entries if row.pairing_id == pairing_id),
            key=lambda row: row.occurred_at,
            reverse=True,
        )[:limit]

    async def prune_recorded(self, *, before: datetime, batch_size: int) -> int:
        stale = sorted(
            (row for row in self.entries if row.occurred_at < before),
            key=lambda row: row.occurred_at,
        )[:batch_size]
        for row in stale:
            self.entries.remove(row)
        return len(stale)


__all__ = [
    "InMemoryCooldownAuditRepository",
    "InMemoryReconciliationTimelineRepository",
]
