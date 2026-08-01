"""The pairing rules — A64-015.3.

Pure and framework-free (architecture.md §8): no clock, no repository, no
Redis, no HTTP. Every input is an argument, so the same arguments always
produce the same pair, and the whole of QT-3 and QT-5 is testable without
a database.

## What "deterministic" has to mean here

Not "usually the same". A pairing scan runs on several workers against one
table, and two workers reading the same pool at the same instant must
reach the same conclusion — otherwise they claim different pairs from the
same tickets and one of them loses a race it did not need to enter.

So there is no `random`, no set iteration, no dependence on the order rows
happened to come back in, and every comparison ends in a total tiebreak.
`PairingEngine.select` re-sorts its input rather than trusting the caller
to have done it: a scan is only as deterministic as its least careful
query.

## The ordering, stated exactly

**Candidates** are ordered by:

    1. entered_at ascending     the longest wait is served first
    2. id ascending             a total tiebreak — UUIDv7, so this is
                                itself time-ordered and two tickets
                                entered in the same microsecond still have
                                one answer

**Partners**, for a given candidate, are ordered by:

    1. |rating difference| ascending    the closest game available
    2. entered_at ascending             then the longest wait
    3. id ascending                     then the total tiebreak

The first candidate with any compatible partner wins, which is what makes
"oldest compatible tickets pair first" true rather than approximately
true. A scan that searched for the globally closest rating match would
starve whoever had waited longest, which is the failure mode a queue is
judged on.

## Why the window is the *narrower* of the two

Two tickets carry two windows, because a window is a function of how long
its own ticket has waited (QT-5). A pair is compatible when the rating gap
fits inside **both**.

The alternative — the wider window governs — would let a player who has
waited five minutes drag in somebody who joined a second ago and asked for
a close game. The wait is one player's; the mismatch would be both
players'. Taking the minimum means a long wait buys *access* to more
opponents, never the right to impose a bad game on one of them.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from app.modules.game.public import PlayerSide
from app.modules.matchmaking.domain.queue_ticket import QueueTicket

#: The UUIDv5 namespace every `pairing_id` is derived in.
#:
#: A fixed, arbitrary URL, and it must never change: a `pairing_id` is the
#: idempotency key `game` deduplicates match creation on, so re-deriving it
#: in a different namespace after a deploy would let one ticket pair create
#: a second match. Recorded here rather than inlined for exactly that
#: reason.
_PAIRING_NAMESPACE = uuid5(NAMESPACE_URL, "https://arena64.dev/matchmaking/pairing")


def pairing_id_for(first: UUID, second: UUID) -> UUID:
    """The stable identifier of the pairing of two queue tickets.

    **Derived, never generated**, which is what makes A64-015.3 §11's
    idempotency contract hold across a process restart. A worker that dies
    after `game` committed the match but before it recorded the outcome
    retries, re-derives the same id, and `game` returns the match it
    already has instead of a second one.

    Order-independent: the two ticket ids are sorted first, so a scan that
    happened to consider the pair the other way round produces the same
    key. Nothing in this module can produce the pair in the other order —
    but nothing should have to be relied on for a value that must be
    stable.

    A UUIDv5 rather than a v7 or a random one, because those are *unique*
    and this must be *reproducible*. A concatenated string would work too;
    a UUID is what the column and the request field already are.
    """
    low, high = sorted((first, second))
    return uuid5(_PAIRING_NAMESPACE, f"{low}:{high}")


@dataclass(frozen=True, slots=True)
class RatingWindowPolicy:
    """How far apart two ratings may be, as a function of waiting — QT-5.

    Configuration rather than a rule the domain invents: the four numbers
    come from `MatchmakingSettings`, so an operator widens a thin pool
    without a deploy and a test can state a policy in one line.

    ## Monotonic, stepped, and bounded

    The width never shrinks as a ticket ages, which is what QT-5 means by
    "expansion" — a policy that narrowed would make a scan's outcome depend
    on *when* it ran rather than on how long people had waited.

    It widens in **steps** rather than continuously, and the step is what
    keeps a scan reproducible: two workers whose clocks differ by a
    millisecond compute the same width for the same ticket everywhere
    except within that millisecond of a step boundary. A continuous
    function would make every scan a different scan.

    And it stops. An unbounded window eventually pairs a beginner with the
    top of the ladder, which is worse for both than telling the beginner
    the pool is thin — the ticket expires, and A64-015.1's `expires_at` is
    already the honest answer to "nobody suitable is here".
    """

    initial_points: int
    """The width a ticket starts with, at age zero."""

    widen_every_seconds: float
    """How long a ticket waits before the width grows by one step."""

    widen_by_points: int
    """How much one step adds."""

    maximum_points: int
    """The width past which it stops growing, however long the wait."""

    def __post_init__(self) -> None:
        if self.initial_points < 0:
            raise ValueError("initial_points cannot be negative")
        if self.widen_every_seconds <= 0:
            raise ValueError("widen_every_seconds must be positive")
        if self.widen_by_points < 0:
            raise ValueError("widen_by_points cannot be negative")
        if self.maximum_points < self.initial_points:
            raise ValueError("maximum_points cannot be below initial_points")

    def width_at(self, waited_seconds: float) -> int:
        """The permitted rating gap for a ticket that has waited this long.

        Clamped below at age zero, because a ticket read by a scan whose
        `now` is a moment behind its `entered_at` is a clock skew rather
        than a ticket from the future — and a negative age must not
        *narrow* the window below its initial value.
        """
        steps = int(max(waited_seconds, 0.0) // self.widen_every_seconds)
        return min(self.initial_points + steps * self.widen_by_points, self.maximum_points)

    def width_for(self, ticket: QueueTicket, *, now: datetime) -> int:
        """The permitted gap for one ticket at one instant."""
        return self.width_at((now - ticket.entered_at).total_seconds())


@dataclass(frozen=True, slots=True)
class PairExclusions:
    """Pairs that must not be formed, whatever their ratings say.

    One value carrying every "these two, never" rule, so `PairingEngine`
    asks one question instead of holding one collaborator per reason. Today
    there are two reasons and they compose by union:

        blocked           BL-2, from `friends.public.PairingExclusions`
        recent opponent   from `RecentOpponentProvider`, deferred

    **Symmetric by construction.** `forbids` checks both directions,
    because the mapping it is built from is allowed to record only one —
    and a rule that held in one direction would pair exactly the halves
    that the other direction missed.
    """

    by_player: Mapping[UUID, frozenset[UUID]] = field(default_factory=dict)

    def forbids(self, one: UUID, other: UUID) -> bool:
        """Whether these two must not be paired."""
        return other in self.by_player.get(one, frozenset()) or one in self.by_player.get(
            other, frozenset()
        )

    @classmethod
    def merged(cls, *sources: Mapping[UUID, frozenset[UUID]]) -> "PairExclusions":
        """One exclusion set from several independent ones.

        Union rather than precedence: every source is a veto, and a pair
        excluded by any of them is excluded. Merging here rather than in
        the service keeps the shape a value's business and means a third
        source is one more argument.
        """
        combined: dict[UUID, set[UUID]] = {}
        for source in sources:
            for player_id, others in source.items():
                if others:
                    combined.setdefault(player_id, set()).update(others)
        return cls({player_id: frozenset(others) for player_id, others in combined.items()})


@dataclass(frozen=True, slots=True)
class TicketPair:
    """Two tickets a scan selected, and the identity of their pairing.

    Not a match. Nothing has been claimed, reserved or created at the point
    this exists — it is the engine's answer, and `PairingService` is what
    turns it into rows.

    `light` and `dark` are the two tickets under the names of the sides
    they were assigned, so the assignment cannot be lost between here and
    `CreateMatchRequest`.
    """

    light: QueueTicket
    dark: QueueTicket
    pairing_id: UUID

    @classmethod
    def of(cls, one: QueueTicket, other: QueueTicket) -> "TicketPair":
        """The pairing of two tickets, with sides assigned.

        ## Why the side comes from the pairing id

        It has to be deterministic — two workers must not disagree about
        who moves first — and it must not be a systematic advantage.

        "The longer wait moves first" is deterministic and is exactly such
        an advantage: light moves first in Russian draughts, so it would
        hand a measurable edge to whoever the pool made wait, in rated
        games, forever. "Lower rating moves first" is worse for the same
        reason and more visibly.

        The parity of `pairing_id` is neither. It is a hash of two
        identifiers neither player chose, it is stable across retries — a
        replayed pairing assigns the same sides as the attempt that
        crashed, which a coin flip could not promise — and over many
        matches it is even.
        """
        pairing_id = pairing_id_for(one.id, other.id)
        first, second = sorted((one, other), key=lambda ticket: ticket.id)
        light_first = pairing_id.int % 2 == 0
        return cls(
            light=first if light_first else second,
            dark=second if light_first else first,
            pairing_id=pairing_id,
        )

    def side_of(self, ticket: QueueTicket) -> PlayerSide:
        """Which side a ticket was assigned. Raises `KeyError` for a ticket
        that is not in this pair."""
        if ticket.id == self.light.id:
            return PlayerSide.LIGHT
        if ticket.id == self.dark.id:
            return PlayerSide.DARK
        raise KeyError(ticket.id)

    def ticket_ids(self) -> tuple[UUID, UUID]:
        """Both ticket ids, light first."""
        return (self.light.id, self.dark.id)

    def player_ids(self) -> tuple[UUID, UUID]:
        """Both player ids, light first."""
        return (self.light.player_id, self.dark.player_id)


class PairingEngine:
    """Chooses at most one pair from one pool's candidates.

    **Stateless**, like every engine collaborator on this platform, so one
    instance serves the process. It holds a `RatingWindowPolicy` because
    that is configuration rather than state — the same policy for every
    call, and a different policy means a different engine.

    **One pair per call, not a maximal matching.** A scan that paired
    everybody it could would hold N row locks and create N matches in one
    transaction, and a failure anywhere would compensate all of them. One
    pair per call means one claim, one match, one compensation — and a pool
    with twenty compatible players drains in twenty ticks of a job that
    runs several times a second. The simpler shape is also the one whose
    failure mode is bounded.
    """

    def __init__(self, window: RatingWindowPolicy) -> None:
        self._window = window

    @property
    def window(self) -> RatingWindowPolicy:
        """The policy this engine applies. Exposed for the log line that
        records why a scan found nothing."""
        return self._window

    def select(
        self,
        candidates: Iterable[QueueTicket],
        *,
        now: datetime,
        exclusions: PairExclusions | None = None,
    ) -> TicketPair | None:
        """The pair this pool yields, or `None` if it yields none.

        `now` is passed rather than read (AD-07): the widening window is a
        function of it, so a scan is reproducible only if the instant is an
        input.

        Tickets that are not `waiting`, or whose window has already closed,
        are dropped before anything is compared. The repository's snapshot
        already excludes both, and dropping them again is what makes this
        method total — an engine that trusted its caller would be a pure
        function with a precondition, which is the kind that eventually
        pairs an expired ticket.

        Returns `None` for an empty pool, a pool of one, and a pool where
        every candidate is excluded from or too far from every other. The
        three are the same answer to the caller and are deliberately not
        distinguished: "no pair this tick" is not a failure, and a scan
        that reported *why* would be reporting the block graph.
        """
        forbidden = exclusions or PairExclusions()
        pool = self._ordered(candidates, now=now)
        if len(pool) < 2:
            return None

        for index, candidate in enumerate(pool):
            partner = self._best_partner(candidate, pool[index + 1 :], now=now, forbidden=forbidden)
            if partner is not None:
                return TicketPair.of(candidate, partner)
        return None

    def _ordered(self, candidates: Iterable[QueueTicket], *, now: datetime) -> list[QueueTicket]:
        """The pairable candidates, oldest first, with a total tiebreak.

        Sorted here rather than trusted from the query, because §3 forbids
        depending on incidental row order and because the in-memory fake
        and PostgreSQL must not be able to disagree.
        """
        return sorted(
            (ticket for ticket in candidates if ticket.is_waiting and not ticket.is_due(now)),
            key=lambda ticket: (ticket.entered_at, ticket.id),
        )

    def _best_partner(
        self,
        candidate: QueueTicket,
        others: Sequence[QueueTicket],
        *,
        now: datetime,
        forbidden: PairExclusions,
    ) -> QueueTicket | None:
        """The closest compatible opponent for `candidate` among `others`.

        `others` is already ordered oldest-first, so a strict `<` on the
        rating gap keeps the *earliest* of any tied set — which is the
        second and third ordering rules applied without a second sort.

        Only later candidates are considered, and that is not a narrowing:
        every pair is reachable from its earlier half, so scanning
        backwards as well would compare each pair twice and change nothing.
        """
        best: QueueTicket | None = None
        best_gap: int | None = None
        width = self._window.width_for(candidate, now=now)

        for other in others:
            if forbidden.forbids(candidate.player_id, other.player_id):
                continue

            gap = abs(candidate.rating_snapshot - other.rating_snapshot)
            # The narrower of the two windows governs — see this module's
            # docstring. Recomputed per opponent because it is a function
            # of *that* ticket's age.
            if gap > min(width, self._window.width_for(other, now=now)):
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = other, gap

        return best


__all__ = [
    "PairExclusions",
    "PairingEngine",
    "RatingWindowPolicy",
    "TicketPair",
    "pairing_id_for",
]
