"""`PairingEngine` and its two policies — A64-015.3.

The engine is pure, so every rule QT-3 and QT-5 state is asserted here with
no database, no clock and no fakes: tickets in, a pair or `None` out.

Ratings and wait times are chosen to make each rule fail on its own. A test
where two tickets are both close in rating *and* the oldest pair would not
tell you which rule produced the answer.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.game.public import PlayerSide, ProductVariant
from app.modules.matchmaking.domain.pairing import (
    PairExclusions,
    PairingEngine,
    RatingWindowPolicy,
    TicketPair,
    pairing_id_for,
)
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType, Region
from app.modules.matchmaking.domain.queue_ticket import QueueTicket

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)

#: A64-015.4's reservation deadline. Thirty seconds, the default
#: `MATCHMAKING_RESERVATION_TTL_SECONDS`, and well inside the ticket's own
#: window — a reservation is a crash-recovery grace, not a wait.
RESERVED_UNTIL = NOW + timedelta(seconds=30)

POOL = QueuePool(variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED)

#: Narrow, so a rating rule fails on its own rather than being masked by a
#: window wide enough to admit everybody.
TIGHT = RatingWindowPolicy(
    initial_points=100, widen_every_seconds=30, widen_by_points=50, maximum_points=300
)


def _ticket(
    *,
    rating: int = 1500,
    waited: float = 0.0,
    player_id: UUID | None = None,
    pool: QueuePool = POOL,
    ticket_id: UUID | None = None,
) -> QueueTicket:
    """A waiting ticket that entered `waited` seconds before `NOW`."""
    entered = NOW - timedelta(seconds=waited)
    return QueueTicket(
        id=ticket_id or generate_uuid7(),
        player_id=player_id or generate_uuid7(),
        pool=pool,
        rating_snapshot=rating,
        entered_at=entered,
        expires_at=entered + TTL,
    )


@pytest.fixture
def engine() -> PairingEngine:
    return PairingEngine(TIGHT)


class TestTheRatingWindow:
    def test_a_fresh_ticket_gets_the_initial_width(self) -> None:
        assert TIGHT.width_at(0) == 100

    def test_it_does_not_widen_before_the_first_step(self) -> None:
        assert TIGHT.width_at(29.9) == 100

    def test_it_widens_by_one_step_at_the_boundary(self) -> None:
        """Stepped rather than continuous — see `RatingWindowPolicy` on why
        two workers a millisecond apart must compute the same width."""
        assert TIGHT.width_at(30) == 150

    def test_it_keeps_widening(self) -> None:
        assert TIGHT.width_at(90) == 250

    def test_it_stops_at_the_maximum(self) -> None:
        """An unbounded window eventually pairs a beginner with the top of
        the ladder. Expiring is the honest answer to a thin pool."""
        assert TIGHT.width_at(10_000) == 300

    def test_it_never_narrows(self) -> None:
        widths = [TIGHT.width_at(seconds) for seconds in range(0, 300, 7)]

        assert widths == sorted(widths)

    def test_a_clock_behind_the_ticket_does_not_narrow_it(self) -> None:
        """Clock skew between two workers is a fact of the deployment. A
        negative age must read as "brand new", never as narrower than the
        initial width."""
        assert TIGHT.width_at(-500) == 100

    def test_a_window_that_narrows_is_refused(self) -> None:
        with pytest.raises(ValueError, match="maximum_points"):
            RatingWindowPolicy(
                initial_points=200, widen_every_seconds=30, widen_by_points=10, maximum_points=100
            )

    def test_a_zero_interval_is_refused(self) -> None:
        """It would be a division by zero dressed as a configuration."""
        with pytest.raises(ValueError, match="widen_every_seconds"):
            RatingWindowPolicy(
                initial_points=100, widen_every_seconds=0, widen_by_points=10, maximum_points=200
            )


class TestCompatibility:
    def test_two_close_ratings_pair(self, engine: PairingEngine) -> None:
        pair = engine.select([_ticket(rating=1500), _ticket(rating=1550)], now=NOW)

        assert pair is not None

    def test_a_gap_outside_the_window_does_not(self, engine: PairingEngine) -> None:
        """§15.3. 400 points apart, both fresh, so both windows are 100."""
        pair = engine.select([_ticket(rating=1200), _ticket(rating=1600)], now=NOW)

        assert pair is None

    def test_the_boundary_is_inclusive(self, engine: PairingEngine) -> None:
        """Exactly the width pairs. A `<` here would make the configured
        number mean one less than it says."""
        pair = engine.select([_ticket(rating=1500), _ticket(rating=1600)], now=NOW)

        assert pair is not None

    def test_one_point_past_the_boundary_does_not(self, engine: PairingEngine) -> None:
        pair = engine.select([_ticket(rating=1500), _ticket(rating=1601)], now=NOW)

        assert pair is None

    def test_waiting_widens_a_pair_into_range(self, engine: PairingEngine) -> None:
        """§15.4. The same two ratings that failed above, after both have
        waited long enough for the window to reach 300."""
        pair = engine.select(
            [_ticket(rating=1200, waited=120), _ticket(rating=1450, waited=120)], now=NOW
        )

        assert pair is not None

    def test_the_narrower_window_governs(self, engine: PairingEngine) -> None:
        """One player has waited two minutes (width 300) and the other just
        joined (width 100). A 250-point gap fits the first window and not
        the second, and the pair is refused — a long wait buys access to
        more opponents, never the right to impose a bad game on one."""
        pair = engine.select(
            [_ticket(rating=1200, waited=120), _ticket(rating=1450, waited=0)], now=NOW
        )

        assert pair is None

    def test_an_identical_rating_always_pairs(self, engine: PairingEngine) -> None:
        pair = engine.select([_ticket(rating=1500), _ticket(rating=1500)], now=NOW)

        assert pair is not None


class TestOrdering:
    def test_the_longest_wait_is_always_served(self, engine: PairingEngine) -> None:
        """§15.2, and the rule is about the *candidate* rather than the
        pair: the oldest ticket is in the answer whenever it has any
        compatible partner at all.

        Two other tickets here are a perfect rating match for each other
        and worse matches for the anchor. A scan that optimised for the
        closest game would pair those two and leave the oldest waiting —
        which is the starvation a queue is judged on.
        """
        oldest = _ticket(rating=1500, waited=300)
        perfect_for_each_other = _ticket(rating=1560, waited=100)
        also_perfect = _ticket(rating=1560, waited=90)

        pair = engine.select([perfect_for_each_other, also_perfect, oldest], now=NOW)

        assert pair is not None
        assert oldest.id in {pair.light.id, pair.dark.id}

    def test_the_oldest_pairs_with_the_oldest_of_its_equal_options(
        self, engine: PairingEngine
    ) -> None:
        """The two rules compose: the anchor is the longest wait, and among
        its equally-close options the longest wait wins again."""
        oldest = _ticket(rating=1500, waited=300)
        older_equal = _ticket(rating=1560, waited=200)
        newer_equal = _ticket(rating=1560, waited=100)

        pair = engine.select([newer_equal, older_equal, oldest], now=NOW)

        assert pair is not None
        assert {pair.light.id, pair.dark.id} == {oldest.id, older_equal.id}

    def test_the_oldest_takes_its_closest_partner(self, engine: PairingEngine) -> None:
        """Within one candidate's options, the closest rating wins."""
        anchor = _ticket(rating=1500, waited=300)
        far = _ticket(rating=1580, waited=200)
        near = _ticket(rating=1510, waited=100)

        pair = engine.select([anchor, far, near], now=NOW)

        assert pair is not None
        assert {pair.light.id, pair.dark.id} == {anchor.id, near.id}

    def test_a_rating_tie_goes_to_the_longer_wait(self, engine: PairingEngine) -> None:
        anchor = _ticket(rating=1500, waited=300)
        older_of_the_tie = _ticket(rating=1520, waited=200)
        younger_of_the_tie = _ticket(rating=1520, waited=10)

        pair = engine.select([anchor, younger_of_the_tie, older_of_the_tie], now=NOW)

        assert pair is not None
        assert {pair.light.id, pair.dark.id} == {anchor.id, older_of_the_tie.id}

    def test_input_order_does_not_change_the_answer(self, engine: PairingEngine) -> None:
        """§15.17, and the reason the engine re-sorts rather than trusting
        the query: a scan is only as deterministic as its least careful
        caller."""
        tickets = [
            _ticket(rating=1500, waited=300),
            _ticket(rating=1520, waited=200),
            _ticket(rating=1505, waited=100),
            _ticket(rating=1900, waited=50),
        ]

        forwards = engine.select(tickets, now=NOW)
        backwards = engine.select(list(reversed(tickets)), now=NOW)

        assert forwards is not None and backwards is not None
        assert forwards.pairing_id == backwards.pairing_id

    def test_the_same_input_always_produces_the_same_pair(self, engine: PairingEngine) -> None:
        """§15.17. Two workers reading one pool at one instant must reach
        one conclusion."""
        tickets = [_ticket(rating=1500 + step * 10, waited=300 - step) for step in range(8)]

        answers = {engine.select(tickets, now=NOW).pairing_id for _ in range(5)}  # type: ignore[union-attr]

        assert len(answers) == 1


class TestExclusions:
    def test_a_blocked_pair_is_skipped(self, engine: PairingEngine) -> None:
        """§15.5. Identical ratings, so nothing but the exclusion can be
        what refused them."""
        one, other = _ticket(rating=1500), _ticket(rating=1500)
        exclusions = PairExclusions({one.player_id: frozenset({other.player_id})})

        assert engine.select([one, other], now=NOW, exclusions=exclusions) is None

    def test_the_exclusion_is_symmetric(self, engine: PairingEngine) -> None:
        """A block is one-directional (BL-1) and its pairing consequence is
        not. Recorded only as other→one, and it must still refuse."""
        one, other = _ticket(rating=1500), _ticket(rating=1500)
        exclusions = PairExclusions({other.player_id: frozenset({one.player_id})})

        assert engine.select([one, other], now=NOW, exclusions=exclusions) is None

    def test_an_excluded_pair_falls_through_to_the_next_candidate(
        self, engine: PairingEngine
    ) -> None:
        """The interesting case: the *best* pair is blocked, so a scan that
        gave up on its first choice would report an empty pool."""
        anchor = _ticket(rating=1500, waited=300)
        blocked = _ticket(rating=1500, waited=200)
        available = _ticket(rating=1530, waited=100)
        exclusions = PairExclusions({anchor.player_id: frozenset({blocked.player_id})})

        pair = engine.select([anchor, blocked, available], now=NOW, exclusions=exclusions)

        assert pair is not None
        assert {pair.light.id, pair.dark.id} == {anchor.id, available.id}

    def test_a_recent_opponent_is_excluded_the_same_way(self, engine: PairingEngine) -> None:
        """§15.7. The two sources are merged before the engine sees them,
        which is why one test covers the second — what is asserted is that
        the merge is a veto, not that a rematch has its own code path."""
        one, other = _ticket(rating=1500), _ticket(rating=1500)
        merged = PairExclusions.merged({}, {one.player_id: frozenset({other.player_id})})

        assert engine.select([one, other], now=NOW, exclusions=merged) is None

    def test_merging_unions_rather_than_overrides(self) -> None:
        alice, bob, carol = generate_uuid7(), generate_uuid7(), generate_uuid7()
        merged = PairExclusions.merged({alice: frozenset({bob})}, {alice: frozenset({carol})})

        assert merged.forbids(alice, bob)
        assert merged.forbids(alice, carol)

    def test_an_empty_exclusion_set_forbids_nothing(self) -> None:
        assert not PairExclusions().forbids(generate_uuid7(), generate_uuid7())


class TestWhatIsNotPairable:
    def test_an_empty_pool_yields_nothing(self, engine: PairingEngine) -> None:
        assert engine.select([], now=NOW) is None

    def test_one_candidate_yields_nothing(self, engine: PairingEngine) -> None:
        assert engine.select([_ticket()], now=NOW) is None

    def test_a_cancelled_ticket_is_never_paired(self, engine: PairingEngine) -> None:
        """§15.10. The snapshot excludes it; the engine excludes it again,
        which is what makes this function total rather than a pure function
        with a precondition."""
        live = _ticket(rating=1500)
        cancelled = _ticket(rating=1500).cancelled(NOW)

        assert engine.select([live, cancelled], now=NOW) is None

    def test_an_expired_ticket_is_never_paired(self, engine: PairingEngine) -> None:
        """§15.11."""
        live = _ticket(rating=1500)
        expired = _ticket(rating=1500).expired(NOW)

        assert engine.select([live, expired], now=NOW) is None

    def test_a_ticket_past_its_deadline_is_never_paired(self, engine: PairingEngine) -> None:
        """Still `waiting` because no sweep has reached it, and still not
        pairable — `expires_at` is the rule and the sweep is bookkeeping."""
        live = _ticket(rating=1500)
        lapsed = _ticket(rating=1500, waited=TTL.total_seconds() + 1)

        assert engine.select([live, lapsed], now=NOW) is None

    def test_a_reserved_ticket_is_never_paired(self, engine: PairingEngine) -> None:
        """Another worker is already creating its match. This is the whole
        reason `reserved` exists as a status a scan cannot see."""
        live = _ticket(rating=1500)
        reserved = _ticket(rating=1500).reserved(until=RESERVED_UNTIL)

        assert engine.select([live, reserved], now=NOW) is None


class TestThePairingIdentity:
    def test_it_is_stable_across_calls(self) -> None:
        """§15.15's foundation: a retry must re-derive the same key, or
        `game` cannot deduplicate."""
        one, other = generate_uuid7(), generate_uuid7()

        assert pairing_id_for(one, other) == pairing_id_for(one, other)

    def test_it_does_not_depend_on_the_order(self) -> None:
        one, other = generate_uuid7(), generate_uuid7()

        assert pairing_id_for(one, other) == pairing_id_for(other, one)

    def test_different_pairs_get_different_identities(self) -> None:
        one, other, third = generate_uuid7(), generate_uuid7(), generate_uuid7()

        assert pairing_id_for(one, other) != pairing_id_for(one, third)


class TestSideAssignment:
    def test_both_sides_are_assigned(self) -> None:
        one, other = _ticket(), _ticket()

        pair = TicketPair.of(one, other)

        assert {pair.light.id, pair.dark.id} == {one.id, other.id}
        assert pair.side_of(pair.light) is PlayerSide.LIGHT
        assert pair.side_of(pair.dark) is PlayerSide.DARK

    def test_the_assignment_is_stable(self) -> None:
        """A replayed pairing must assign the same sides as the attempt that
        crashed — which a coin flip could not promise."""
        one, other = _ticket(), _ticket()

        first, again = TicketPair.of(one, other), TicketPair.of(other, one)

        assert first.light.id == again.light.id
        assert first.dark.id == again.dark.id

    def test_it_does_not_follow_the_longer_wait(self) -> None:
        """Light moves first in Russian draughts, so "the longer wait moves
        first" would be a measurable, permanent edge handed to whoever the
        pool made wait. Over enough pairs the assignment must not correlate
        with wait time."""
        light_was_older = 0
        for _ in range(200):
            older, newer = _ticket(waited=300), _ticket(waited=1)
            pair = TicketPair.of(older, newer)
            light_was_older += pair.light.id == older.id

        assert 40 < light_was_older < 160

    def test_a_foreign_ticket_has_no_side(self) -> None:
        pair = TicketPair.of(_ticket(), _ticket())

        with pytest.raises(KeyError):
            pair.side_of(_ticket())


class TestPoolsDoNotMix:
    """§15.16. The engine never sees two pools, and this states why: the
    separation is the *query's* — `queue_snapshot` filters on all three
    columns — not something the engine is asked to re-check.

    Asserted at the boundary rather than inside `select`, because an engine
    that filtered by pool would be an engine that could be called with a
    mixed batch, which is exactly the shape §1 forbids.
    """

    def test_a_pool_is_the_whole_of_what_makes_two_players_candidates(self) -> None:
        ranked = QueuePool(variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED)
        casual = QueuePool(variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.CASUAL)

        assert ranked != casual

    def test_a_region_makes_a_different_pool(self) -> None:
        europe = QueuePool(
            variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED, region=Region.EUROPE
        )
        asia = QueuePool(
            variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED, region=Region.ASIA
        )

        assert europe != asia
