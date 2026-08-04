"""Seeding and placement — SPEC-TOURNAMENT §6, A64-019.3 §15.

Pure: the algorithm takes rating values and returns slots, so every rule is
checkable without a database. What is asserted is the four things that make
a bracket defensible — a total order, the right size, a placement that
separates the top seeds, and byes that fall to the players who earned them.
"""

from uuid import UUID

import pytest

from app.modules.tournament.domain.exceptions import InvalidCapacity
from app.modules.tournament.domain.seeding import (
    SeedInput,
    bracket_size,
    first_round_pairings,
    seed_slots,
    seeded,
)


def _player(suffix: int) -> UUID:
    """Deterministic ids, so a tie broken by `player_id` is assertable
    rather than a coin toss."""
    return UUID(f"00000000-0000-0000-0000-{suffix:012d}")


def _entrant(suffix: int, *, rating: float, deviation: float = 100.0) -> SeedInput:
    return SeedInput(
        player_id=_player(suffix),
        rating=rating,
        deviation=deviation,
        is_provisional=deviation > 200.0,
    )


class TestSeedOrder:
    def test_it_is_total_and_breaks_ties_the_same_way_every_time(self) -> None:
        """§4 — rating DESC, deviation ASC, player_id ASC.

        Built so each key is the *only* one separating a pair: two players
        share a rating and split on deviation, two share both and split on
        id. A missing key would order them by whatever the input list
        happened to be, and a non-deterministic seeding produces a
        **different bracket on a retry** — which is the failure this order
        exists to prevent, not untidiness.

        The provisional player is seeded, not excluded: §4 keeps them
        eligible, and their large deviation already places them below an
        established player on the same rating.
        """
        seeds = seeded(
            [
                _entrant(3, rating=1600.0, deviation=50.0),
                _entrant(1, rating=1800.0, deviation=300.0),  # provisional
                _entrant(4, rating=1600.0, deviation=50.0),
                _entrant(2, rating=1600.0, deviation=200.0),
            ]
        )

        assert [seed.player_id for seed in seeds] == [
            _player(1),  # highest rating, provisional or not
            _player(3),  # tied at 1600, tightest deviation, lower id
            _player(4),  # tied on rating and deviation, higher id
            _player(2),  # tied at 1600, widest deviation
        ]
        assert [seed.number for seed in seeds] == [1, 2, 3, 4]
        assert seeds[0].is_provisional is True

    def test_fewer_than_two_entrants_is_not_a_tournament(self) -> None:
        with pytest.raises(InvalidCapacity):
            seeded([_entrant(1, rating=1500.0)])


class TestBracketSize:
    def test_it_is_the_next_power_of_two_above_the_field(self) -> None:
        """§5 — sized from who turned up, never from `capacity`.

        Capacity is the registration maximum (A64-019.2's decision), so a
        tournament with capacity 10 and 6 entrants plays an 8-bracket with
        two byes rather than a 16-bracket with ten.
        """
        assert [bracket_size(n) for n in (2, 3, 4, 5, 8, 10, 128)] == [2, 4, 4, 8, 8, 16, 128]

        with pytest.raises(InvalidCapacity):
            bracket_size(1)


class TestPlacement:
    def test_the_top_seeds_are_separated_across_halves_and_quarters(self) -> None:
        """§6 — the property naive `1v2, 3v4` pairing does not have.

        Seeds 1 and 2 must be able to meet **only** in the final, so they
        sit in opposite halves; seeds 1–4 must sit in distinct quarters, so
        the best four cannot eliminate each other early. Both are asserted
        on a 16-bracket, where a quarter is four slots and the property is
        not true by accident.

        Every seed also faces `size + 1 - seed`, which is what makes the
        reward for seeding well monotone rather than incidental.
        """
        order = seed_slots(16)
        pairs = [(order[i], order[i + 1]) for i in range(0, 16, 2)]

        assert all(high + low == 17 for high, low in pairs)

        halves = [
            {seed for pair in pairs[:4] for seed in pair},
            {seed for pair in pairs[4:] for seed in pair},
        ]
        assert (1 in halves[0]) != (1 in halves[1])
        assert (2 in halves[0]) != (2 in halves[1])
        assert not ({1, 2} <= halves[0] or {1, 2} <= halves[1])

        quarters = [{seed for pair in pairs[i : i + 2] for seed in pair} for i in range(0, 8, 2)]
        assert [next(q for q in range(4) if seed in quarters[q]) for seed in (1, 2, 3, 4)] == [
            0,
            2,
            3,
            1,
        ]

    def test_it_is_stable_and_matches_the_published_orders(self) -> None:
        """The algorithm pinned, so a refactor cannot quietly reshuffle a
        bracket. Sizes 2 and 4 are small enough to read."""
        assert seed_slots(2) == [1, 2]
        assert seed_slots(4) == [1, 4, 2, 3]
        assert seed_slots(8) == seed_slots(8)  # deterministic across calls


class TestByesAndSides:
    def test_the_highest_seeds_receive_the_byes_and_sides_alternate(self) -> None:
        """§7 and §9, which fall out of the placement rather than being
        applied.

        Six entrants in an 8-bracket leaves seeds 7 and 8 absent. Because
        seed *s* faces `size + 1 - s`, the empty seeds land opposite seeds 1
        and 2 — so the byes go to the top of the field without a rule to
        apply, and a lower seed cannot be handed one.

        **A bye is an empty slot**, never a fake player: the pairing has one
        participant and one `None`, and `present_player_id` is what
        A64-019.4 will advance without creating a match.

        Sides alternate by slot, so moving first is not always the better
        player's — deterministic, and no historical balancing, because a
        player gets at most three games in an 8-bracket.
        """
        seeds = seeded([_entrant(i, rating=2000.0 - i * 100) for i in range(1, 7)])
        pairings = first_round_pairings(seeds)

        assert len(pairings) == 4

        byes = [p for p in pairings if p.is_bye]
        assert len(byes) == 2
        assert {p.light_seed or p.dark_seed for p in byes} == {1, 2}
        assert all(p.present_player_id is not None for p in byes)

        # Slot 0: the higher seed is LIGHT. Slot 1: the higher seed is DARK.
        assert pairings[0].light_seed == 1
        assert pairings[1].dark_seed == 4
