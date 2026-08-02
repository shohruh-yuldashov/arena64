"""`ReconciliationEntry` — what recovery did to one queue ticket, kept where
an operator can find it. A64-015.6 §4.

A64-015.5 published `matchmaking.pairing_reconciled` and recorded the gap in
its own recommendations:

> "`PairingReconciled` has no consumer. It is written on every recovery and
> read by nobody — a consumer turning it into an operator-facing timeline is
> cheap now that the events carry ticket ids."

This is that timeline. It is the projection side of an event that was already
durable, so nothing about recovery changes; what changes is that "why did this
player's ticket go back into the queue at 03:12" stops being a log-aggregator
archaeology exercise.

## Why a projection and not a log query

The log line exists (`pairing_reconciled`, one per batch) and is the wrong
tool for the question. It is aggregated per tick, so it says *five tickets
were settled* and not *which*; it is retained on the log pipeline's horizon
rather than the platform's; and it cannot be joined to a ticket id, which is
the only identifier a support conversation starts from.

The event carries the ticket, the action, the match and the deadline it
overran. A relation keyed on the ticket turns all four into one indexed
lookup — AD-19's "every projection is rebuildable from PostgreSQL" applies
directly, because the outbox rows it is built from are the durable source.

## Operations, not a product surface

§4: "This timeline is for operations and support, not a public API." There is
no route, no schema and no `presentation` type for it in this task, and that
is deliberate rather than deferred: a player-facing reconciliation history is
listed in A64-015.6's own out-of-scope, and the identifiers here — ticket,
pairing, match — are internal ones a player has no use for and an attacker
would.

What it carries about a person is a `player_id`, which is DM-06's opaque
identifier and is already on every row of `queue_ticket`. No handle, no
display name, no address: the timeline answers "what happened to this ticket",
and resolving the ticket to a person is `users`' job and a separate decision.

## Bounded, like everything else recovery writes

One row per reconciled ticket, on its own retention horizon
(`MATCHMAKING_TIMELINE_RETENTION_HOURS`). It is a projection of an event that
is itself pruned, so keeping it longer than the outbox would be keeping a
derivative of something the platform has forgotten.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.matchmaking.domain.events import PairingReconciled, ReconciliationAction


@dataclass(frozen=True, slots=True)
class ReconciliationEntry:
    """One recovery action, as an operator reads it.

    Frozen and never updated, for the reason `CooldownRecord` is: an audit row
    that can be edited answers "what does the platform say now" rather than
    "what happened".
    """

    event_id: UUID
    """The outbox entry this was projected from.

    **The idempotency key.** A unique index on it means a redelivered
    `pairing_reconciled` — which AD-16's at-least-once contract guarantees
    will happen — produces one row rather than two. It is also the join back
    to the event, which is what makes the projection rebuildable (AD-19).
    """

    ticket_id: UUID
    """Which queue ticket this happened to. The identifier a support
    conversation starts from, and the reason this is a relation rather than a
    log query."""

    player_id: UUID
    """Whose ticket. DM-06's opaque identifier — see this module's docstring
    on what is deliberately absent."""

    action: ReconciliationAction
    match_id: UUID | None
    """The match the ticket turned out to have, or `None`. Non-null exactly
    when `action` is `settled` — which is the whole content of "was this
    player's game already created when we found their ticket stranded"."""

    pairing_id: UUID | None
    """The pairing, when the event carried one.

    `None` today for every action: `PairingReconciled` identifies a *ticket*
    rather than a pairing, because the reconciler claims whatever bounded
    batch it locks and may hold one half of a pair without the other. The
    column exists because §4 requires the timeline to be queryable by pairing
    identifier and a nullable column with an index is cheaper to add now than
    a migration later — and it is honestly empty rather than back-filled with
    a guess.
    """

    occurred_at: datetime
    """When the fact became true — the deadline the reservation overran,
    carried from the event rather than the instant this row was written. The
    same rule `PairingReconciled.occurred_at` follows, and it is what keeps a
    timeline ordered by *what happened* rather than by when the relay caught
    up."""

    recorded_at: datetime
    """When the projection saw it. Kept beside `occurred_at` rather than
    instead of it, because the gap between the two is relay lag — which is
    exactly what an operator investigating "why was this late" wants, and is
    not derivable from either alone."""

    id: UUID = field(default_factory=generate_uuid7)

    @property
    def is_failure(self) -> bool:
        """Whether this entry describes recovery itself failing.

        `reconciliation_failed` is the one action that is not a *resolution* —
        the others say what became of a ticket, and this one says the tick
        could not say. An operator filtering a timeline wants it separated.
        """
        return self.action is ReconciliationAction.FAILED

    @classmethod
    def of(
        cls, event: PairingReconciled, *, event_id: UUID, recorded_at: datetime
    ) -> "ReconciliationEntry":
        """The timeline row for one `pairing_reconciled`.

        Built from the **event payload** rather than by re-reading the ticket,
        which is what makes the projection correct when it runs late: by then
        the ticket may have been paired again, or pruned, and the answer to
        "what did recovery do" would have changed underneath it.
        """
        return cls(
            event_id=event_id,
            ticket_id=event.ticket_id,
            player_id=event.player_id,
            action=event.action,
            match_id=event.match_id,
            pairing_id=None,
            occurred_at=event.occurred_at,
            recorded_at=recorded_at,
        )


__all__ = ["ReconciliationEntry"]
