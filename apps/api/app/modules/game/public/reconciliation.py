"""What a match somebody else asked for turned out to be.

Two readers for one shape of question — "did the work I handed `game`
actually happen" — keyed differently because their callers hold different
things. `PairingReconciliationReader` (A64-015.4 §9) is keyed by queue
ticket; `OriginMatchReader` (A64-019.5) is keyed by R-25's opaque
`origin_ref`, for the contexts that have no ticket to key on.

## What a reserved queue ticket's match turned out to be — A64-015.4 §9

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
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.engine import PlayerSide
from app.modules.game.domain.variants import MatchOrigin


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


class OriginMatchState(StrEnum):
    """How a match stands, in the vocabulary an originating context acts on.

    A **purpose-built closed enum on a published view**, which is the same
    line `MatchRecordStatus` sits on: it says what happened without handing
    out anything that can change a match. Deliberately not
    `MatchRecordStatus`, and not `MatchOutcome` either — a reconciler asks
    one question, "may I move on, and to what", and answering it from two
    enums means every caller re-deriving the join between them.

    Exhaustive over every state a match can be in, so a caller matching on
    it has no fall-through.
    """

    LIVE = "live"
    """Created or being played. Nothing to reconcile — wait."""

    DECIDED = "decided"
    """Played to a result with a winner. `winner` names the side."""

    DRAWN = "drawn"
    """Played to a result with no winner. A context that needs one — a
    single-elimination bracket — has to decide what that means; `game` has
    no opinion."""

    ABANDONED = "abandoned"
    """Ended with no result at all: declined, expired, or aborted.

    Distinguished from `DRAWN` because the two are opposite facts about the
    same absence of a winner — a draw is a game both players finished, and
    this is a game that was never played. MT-11 keeps the second out of
    every rating and statistic, and a context that collapsed them would
    record an abandonment as a result.
    """


@dataclass(frozen=True, slots=True)
class OriginMatchOutcome:
    """What became of one match an originating context asked for.

    Primitive-only but for two closed enums, like every other published
    view here. It carries no seats, no ratings and no move log: the
    question is "did the match I asked for happen, and how did it end",
    and anything more would be a projection a caller could build a second
    history from.
    """

    match_id: UUID
    origin: MatchOrigin
    origin_ref: UUID
    """The identifier the originating context supplied. Non-optional here
    because a match without one cannot be keyed by it, so it is never in a
    result."""

    state: OriginMatchState
    winner: PlayerSide | None
    """Which side won, and `None` unless `state` is `DECIDED`.

    The pairing is the enum's invariant rather than a caller's assumption —
    `MatchResult` holds the same one, and `game`'s `CHECK` holds it in the
    table.
    """

    created_at: datetime
    """When `game` committed the match.

    Carried because a reference may have several matches and the caller has
    to put them back in order — a tournament pairing's rematch is by
    definition the later one, and there is nothing else on this view that
    says so.
    """

    ended_at: datetime | None
    """When the contest ended, or `None` while it has not."""


class OriginMatchReader(Protocol):
    """`game`'s answer to "what became of the matches I asked for".

    The counterpart to `PairingReconciliationReader` for the contexts that
    are **not** the queue, and it exists for exactly the reason that one
    does: creating a match and recording that it was created are two
    transactions BE-05 forbids collapsing, so there is a window in which
    `game` has a match the originating context does not know about. The
    queue closes that window by asking about its tickets; a tournament
    cannot, because it has none.

    R-25 gave the originating context an opaque `origin_ref` to key on, and
    A64-019.5 makes it answerable. Read-only and batched: a caller can
    learn what happened to its own matches and can change nothing.

    ## Why it is keyed by `origin_ref` rather than by `match_id`

    A reconciler's hardest case is the one where it does **not** have the
    match id — the worker died after `game` committed and before anything
    recorded it. Keying on the reference the caller supplied is what makes
    that case answerable at all; keying on the match id would answer only
    the cases that are already half-recovered.
    """

    async def outcomes_for(
        self, origin_refs: Sequence[UUID], *, origin: MatchOrigin
    ) -> Sequence[OriginMatchOutcome]:
        """What became of the matches created for these references.

        **A sequence, not a mapping keyed by reference**, and the difference
        is load-bearing: one reference may have produced more than one
        match. A tournament pairing that drew plays a rematch under the same
        `origin_ref` (SPEC-TOURNAMENT §6c), and a mapping would silently
        return one of the two — which is the shape of bug that looks like a
        working reconciler until the first draw.

        `origin` is required rather than inferred, so one context cannot
        read another's matches by guessing a reference — the pair is the
        key, and `ix_match__origin_ref` is the index over exactly it.

        A reference with no entry produced no match. That is the ordinary
        answer for work that was planned and never launched, and the case
        whose action is "launch it now".

        **Batched**, and that is correctness rather than speed for the same
        reason `settlements_for` is: a reconciler claims a bounded page per
        tick, and a query per reference would make the recovery job the
        N+1 it exists to avoid.

        **Propagates.** There is no safe default: guessing "no match"
        creates a second game for two players who already have one, and
        guessing "not finished" stalls a bracket. The caller's correct
        response to an unreadable answer is to fail the tick and try again.
        """
        ...


__all__ = [
    "OriginMatchOutcome",
    "OriginMatchReader",
    "OriginMatchState",
    "PairingReconciliationReader",
    "PairingSettlement",
]
