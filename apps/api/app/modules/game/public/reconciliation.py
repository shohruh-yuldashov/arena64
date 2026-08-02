"""What a reserved queue ticket's match turned out to be — A64-015.4 §9.

The read that makes automatic reconciliation possible, and the reason it
has to exist is a boundary rather than a convenience.

Pairing is two writes that cannot share a transaction: `game` commits a
match, then `matchmaking` marks two tickets `matched`. services.md BE-05
forbids collapsing them — a cross-context call inside an open transaction
holds two row locks across another module's work — so there is a window in
which the match exists and the tickets do not say so. A64-015.3 shipped
that window with a `pairing_settle_failed` log line and a human on the end
of it.

Closing it needs one fact that `matchmaking` cannot hold: **did this
ticket's match get created?** The ticket has no `match_id` — it could not,
because the match is written after the ticket is reserved — and the answer
lives in `game`'s table. So `game` publishes it, keyed by the ticket id
`MatchSeat` already records as provenance.

## Why keyed by ticket and not by pairing

A `pairing_id` is derived from *both* ticket ids, and a reconciler holding
one orphaned reserved ticket does not know the other. Keying on the ticket
means each row is reconcilable on its own, which is the property that makes
the job safe to run in bounded batches over whatever it happens to claim.

## What it deliberately does not return

No status, no acceptance state, no deadline. The reconciler's question is
strictly "does a match exist for this ticket, and when was it created" —
because the ticket's transition is the same whether that match is pending,
active, cancelled or expired. A ticket that produced a match is `matched`;
what happened to the match afterwards is the match's business, and giving
the queue an opinion about it would be a second place the acceptance
lifecycle is interpreted.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PairingSettlement:
    """The match one reserved queue ticket produced."""

    match_id: UUID
    pairing_id: UUID
    created_at: datetime
    """When the match was committed.

    Carried because it is the instant `QueueTicket.matched` records, and
    "when did this player's game start" must not become "when did the
    reconciler get round to it" just because a worker died in between.
    """


class PairingReconciliationReader(Protocol):
    """`game`'s answer to "was a match created for these tickets"."""

    async def settlements_for(self, ticket_ids: Sequence[UUID]) -> Mapping[UUID, PairingSettlement]:
        """The match each of `ticket_ids` produced, for those that produced
        one.

        **Batched**, and that is not an optimisation: the reconciler claims
        a bounded page of stale reservations per tick, and one query per
        ticket would make the recovery job itself the N+1 the batch exists
        to avoid.

        A ticket with no entry produced no match — which is the ordinary
        answer for a reservation whose worker died before it called `game`,
        and the case whose action is "put this player back in the queue".
        """
        ...


__all__ = ["PairingReconciliationReader", "PairingSettlement"]
