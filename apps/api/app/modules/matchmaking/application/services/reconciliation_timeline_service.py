"""`ReconciliationTimelineProjector` — the consumer `PairingReconciled` did
not have. A64-015.6 §4.

A64-015.5 published the event and said so in its own recommendations:
"`PairingReconciled` has no consumer. It is written on every recovery and read
by nobody." This is the consumer, and it is the smallest useful one: it
projects each event into a row keyed on the queue ticket, and does nothing
else.

## What it is for

An operator or a support agent holding a ticket id and the question "why did
this player's ticket go back into the queue at 03:12". Before this, the
answer lived in a `pairing_reconciled` log line that is aggregated per tick —
it says *five tickets were settled* and not which — on the log pipeline's
retention rather than the platform's, and unjoinable to a ticket.

## What it is deliberately not

**Not a product surface.** No route, no schema, no `presentation` type; a
player-facing reconciliation history is in A64-015.6's own out-of-scope list.
The identifiers it holds are internal ones a player has no use for.

**Not a metric.** The counter already exists
(`matchmaking.reconciliation_actions_total`) and answers "how often"; this
answers "which one, and when". A64-015.5 §9 forbids putting a ticket id in a
label, and this relation is the place that question belongs instead.

**Not a second source of truth.** It is a projection (AD-19): the outbox rows
it is built from are durable and `event_id` is the join back to them, so a
corrupted timeline is rebuilt by replaying rather than reconciled by hand.

## Idempotency

`uq_pairing_timeline__event`. The ledger stops a redelivered entry reaching
this consumer at all in the ordinary case, and cannot stop two relays
delivering concurrently — so the constraint is what actually holds, and the
repository's `ON CONFLICT DO NOTHING` turns a duplicate into a no-op rather
than a second row.

Nothing here accumulates or counts between entries, so processing a batch
twice produces the same relation.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.matchmaking.application.ports import ReconciliationTimelineRepository
from app.modules.matchmaking.domain.events import PairingReconciled, ReconciliationAction
from app.modules.matchmaking.domain.reconciliation_timeline import ReconciliationEntry
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: This consumer's name in `platform.processed_event`.
#:
#: Renaming it replays every retained `pairing_reconciled` into the timeline.
#: That is *safe* here, unlike for the acceptance-failure policy — the
#: projection is idempotent on `event_id` and a replay is how AD-19 says a
#: projection is rebuilt — but it is still a migration rather than a rename.
CONSUMER_NAME = "matchmaking_reconciliation_timeline"

#: The one event this consumer subscribes to.
SUBSCRIBED_EVENT_TYPES: frozenset[str] = frozenset({PairingReconciled.event_type})


@dataclass(frozen=True, slots=True)
class _Failed:
    """One entry this consumer could not project — `outbox.ports.EventFailure`."""

    entry_id: UUID
    reason: str


class ReconciliationTimelineProjector:
    """Turns recovery events into an operator-readable timeline.

    Holds a repository, a unit of work and a clock, and nothing else. It has
    no cross-module port at all — which is what makes it the cheapest consumer
    on the relay and the one least able to be slow.
    """

    def __init__(
        self,
        *,
        timeline: ReconciliationTimelineRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._timeline = timeline
        self._unit_of_work = unit_of_work
        self._clock = clock

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        return event_type in SUBSCRIBED_EVENT_TYPES

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failed]:
        """Projects a batch. Returns the entries that failed.

        **One transaction for the whole batch**, unlike the acceptance-failure
        policy beside it. That consumer writes per player and its outcomes
        depend on each other's state; this one writes independent rows keyed
        on independent events, so there is nothing an interleaving could get
        wrong and a transaction per entry would be a commit per row.

        A malformed payload fails its own entry and the batch continues,
        because one producer bug must not stop the timeline recording
        everything else.
        """
        projected: list[ReconciliationEntry] = []
        failures: list[_Failed] = []
        recorded_at = self._clock.now()

        try:
            async with self._unit_of_work:
                for entry in entries:
                    try:
                        projected.append(
                            await self._timeline.append(_entry_for(entry, recorded_at=recorded_at))
                        )
                    except (KeyError, ValueError) as error:
                        # A payload this consumer cannot read is a producer
                        # bug. It fails its own entry rather than the batch,
                        # and the relay backs that entry off on its own.
                        logger.warning(
                            "reconciliation_timeline_payload_rejected",
                            extra={
                                "event_id": str(entry.id),
                                "error": type(error).__name__,
                            },
                        )
                        failures.append(_Failed(entry_id=entry.id, reason=type(error).__name__))
                await self._unit_of_work.commit()
        except Exception as error:  # noqa: BLE001 — a storage failure is retryable
            logger.warning(
                "reconciliation_timeline_write_failed",
                extra={"entries": len(entries), "error": type(error).__name__},
                exc_info=error,
            )
            return [_Failed(entry_id=entry.id, reason=type(error).__name__) for entry in entries]

        if projected:
            # One line per batch rather than per entry — a deploy that
            # stranded two hundred reservations would otherwise emit two
            # hundred records (CLAUDE.md §8.8). The per-entry detail is the
            # relation itself, which is the point of having one.
            logger.info(
                "reconciliation_timeline_projected",
                extra={
                    "entries": len(projected),
                    "failures": sum(1 for row in projected if row.is_failure),
                },
            )
        return failures


def _entry_for(entry: OutboxEntry, *, recorded_at: datetime) -> ReconciliationEntry:
    """One outbox payload as a timeline row.

    Built from the **payload** rather than by re-reading the ticket, which is
    what makes the projection correct when it runs late: by then the ticket
    may have been paired again or pruned, and "what did recovery do" would
    have changed underneath it.

    Raises `KeyError` or `ValueError` for a payload this consumer cannot read,
    which the caller turns into a recorded per-entry failure.
    """
    payload = entry.payload
    match_id = payload.get("match_id")
    return ReconciliationEntry(
        event_id=entry.id,
        ticket_id=UUID(str(payload["ticket_id"])),
        player_id=UUID(str(payload["player_id"])),
        action=ReconciliationAction(str(payload["action"])),
        match_id=None if match_id is None else UUID(str(match_id)),
        pairing_id=None,
        occurred_at=entry.occurred_at,
        recorded_at=recorded_at,
    )


__all__ = [
    "CONSUMER_NAME",
    "SUBSCRIBED_EVENT_TYPES",
    "ReconciliationTimelineProjector",
]
