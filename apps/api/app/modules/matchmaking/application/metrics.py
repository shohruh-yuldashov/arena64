"""Every metric `matchmaking` and its half of the handshake emit —
A64-015.5 §7 and §9.

One catalogue rather than string literals at call sites, for the reason
`DomainEvent.event_type` is a class attribute: a metric name typed twice is
a dashboard that silently plots half the traffic, and unlike a wrong event
type nothing fails when it happens.

## What §7 is actually asking for

The thirty-second acceptance deadline (`MATCHMAKING_RESERVATION_TTL_SECONDS`)
is a **product assumption**, written down as one in A64-015.4 and inherited
unchanged. §7 forbids moving it on intuition and requires the measurement
first. These are that measurement:

| Metric | Answers |
| --- | --- |
| `game.match_answer_latency_seconds{outcome}` | how long players actually take |
| `game.match_outcomes_total{outcome}` | how often each ending happens |

Both are emitted by `game` and declared in `game.public.metrics`, because
`MatchAcceptanceService` is what holds both `created_at` and the instant of
the answer. They are published from there rather than kept private for
exactly this reason: the setting they inform is `matchmaking`'s, so the
tuning process below has to be able to name them.

**The tuning process, stated so it is not reinvented.** Let the histogram
run for a period that covers a weekend — acceptance latency is a human
behaviour and weekday evenings are not the whole distribution. Then:

  - if `p99` of `first_response` sits well inside the window, the deadline
    is generous and may come down; the constraint on lowering it is the
    `expired` share, which must not rise;
  - if `expired` is a material share of `match_outcomes_total` *and* the
    `first_response` histogram has a long tail, the window is too short and
    people are being timed out mid-decision;
  - if `expired` is material and the histogram has **no** tail, the players
    are not there at all, and the answer is presence rather than a longer
    deadline.

The third case is the one a shorter deadline would misdiagnose, and it is
why the counters and the histogram have to be read together.

## Why the label sets are closed enumerations

§9 forbids high-cardinality labels — no player ids, no match ids, no
pairing ids. Every label value below comes from a `StrEnum` in this file,
so the number of time series each metric can produce is fixed at import
time and visible here. A test asserts it.
"""

from enum import StrEnum

#: What the reconciler did with one stranded reservation — §9.
RECONCILIATION_ACTIONS = "matchmaking.reconciliation_actions_total"

#: How a player who lost a match to somebody else's answer was treated.
#: The acceptance-failure policy (§1), as a number.
ACCEPTANCE_FAILURE_ACTIONS = "matchmaking.acceptance_failure_actions_total"

#: Realtime pending-match delivery — §4. Counted by what happened to the
#: attempt rather than by recipient.
PENDING_MATCH_DELIVERIES = "matchmaking.pending_match_deliveries_total"

#: Rows removed by retention, by relation — §8.
RETENTION_DELETIONS = "matchmaking.retention_deletions_total"


class AcceptanceFailureAction(StrEnum):
    """What §1's policy did to one participant of a failed handshake.

    Per **player**, not per match, because the whole point of the policy is
    that the two participants are treated differently.
    """

    REQUEUED = "requeued"
    """They accepted, the match failed anyway, and they went back into the
    queue with the `entered_at` they always had."""

    REQUEUE_SKIPPED = "requeue_skipped"
    """They accepted, and the requeue did not apply — they already hold a
    live ticket, or they are no longer eligible. Not a failure; see
    `QueueService.requeue`."""

    COOLDOWN_APPLIED = "cooldown_applied"
    """They declined explicitly, and a cooldown was recorded."""

    NO_ACTION = "no_action"
    """They answered nothing and accepted nothing. Silence is not a
    decline (§3), so it earns neither a requeue nor a cooldown."""


class DeliveryOutcome(StrEnum):
    """What became of one realtime pending-match delivery attempt — §4, §6."""

    DELIVERED = "delivered"
    STALE = "stale"
    """The match was answered, expired or cancelled between the event being
    written and this consumer reading it. §6's whole point: enqueue-time
    state is not trusted."""

    DEADLINE_PASSED = "deadline_passed"
    """Still pending, but there is no longer time to answer. Delivering it
    would put a countdown on a client's screen that starts below zero."""

    PREVIEW_WITHHELD = "preview_withheld"
    """Delivered, with the opponent preview omitted because a block now
    exists between the two. The match is still answerable — see
    `PendingMatchNotifier`."""


class RetentionRelation(StrEnum):
    """Which relation a retention run deleted from."""

    QUEUE_TICKET = "queue_ticket"
    ABANDONED_MATCH = "abandoned_match"


__all__ = [
    "ACCEPTANCE_FAILURE_ACTIONS",
    "PENDING_MATCH_DELIVERIES",
    "RECONCILIATION_ACTIONS",
    "RETENTION_DELETIONS",
    "AcceptanceFailureAction",
    "DeliveryOutcome",
    "RetentionRelation",
]
