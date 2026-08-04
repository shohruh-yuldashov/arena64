"""Seeding and single-elimination placement — SPEC-TOURNAMENT §6.

Pure arithmetic over a list of players. No clock, no database, no rating
reader: the *inputs* are rating snapshots somebody else fetched, and that is
what makes every rule here testable and every bracket reproducible.

## Seed order is total, and that is the point

    rating DESC, deviation ASC, player_id ASC

Three keys, because two are not enough to be deterministic and a
non-deterministic seeding produces a *different bracket* on a retry. The
third is unique, so no two players compare equal and the same input always
yields the same bracket.

Deviation second, for the reason the leaderboard uses it: between two
players on the same rating, the one the platform is more sure about seeds
higher. Provisional players are seeded, not excluded — §4 keeps them
eligible and their large deviation already places them below an established
player on the same number.

## Bracket size is the next power of two

`ceil` to a power of two, never `tournament.capacity`. Capacity is the
maximum *registration* count (A64-019.2's decision); the bracket is sized
from who actually turned up, so a tournament with capacity 10 and 6 entrants
plays an 8-bracket with 2 byes rather than a 16-bracket with 10.

## Placement: the standard recursive doubling

    order(1)  = [1]
    order(2n) = interleave(order(n), [2n + 1 - s for s in order(n)])

    size 2    [1, 2]
    size 4    [1, 4, 3, 2]
    size 8    [1, 8, 5, 4, 3, 6, 7, 2]

The list is seed numbers **in bracket-slot order**; slot `2j` plays slot
`2j+1`. What the construction guarantees, and what a naive `1v2, 3v4` does
not:

    seeds 1 and 2 meet only in the final   they are at opposite ends, so
                                           their halves never intersect
    seeds 1-4 are in distinct quarters      each doubling splits the
                                           previous order across the new
                                           halves
    the reward is monotone                 seed *s* always faces
                                           `size + 1 - s`, so the best seed
                                           draws the worst opponent

**Byes fall out of it.** A seed higher than the entrant count has no player,
so the pairing has one participant and an empty slot — and because seed
`size` is opposite seed 1, the empty seeds land against the *highest* seeds
first, which is §7's "highest seeds receive byes first" without a rule to
apply.

## Side assignment

The higher seed takes `LIGHT` on even slots and `DARK` on odd ones.
Deterministic, and alternating so the advantage of moving first is spread
across the bracket rather than always given to the better player. No
historical colour balancing: single elimination gives a player at most
`log2(size)` games, so there is no history to balance.
"""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.modules.tournament.domain.exceptions import InvalidCapacity

#: Seed numbers start at 1 — an ordinal, like round numbers and ply numbers.
FIRST_SEED: Final = 1

#: Fewer than two entrants is not a tournament.
MIN_ENTRANTS: Final = 2


@dataclass(frozen=True, slots=True)
class SeedInput:
    """One entrant's rating, as seeding reads it.

    A value rather than `rating.public.RatingSnapshot` so this module stays
    pure: the caller fetches snapshots in one batch and converts, and the
    algorithm depends on four numbers rather than on another module's type.
    """

    player_id: UUID
    rating: float
    deviation: float
    is_provisional: bool


@dataclass(frozen=True, slots=True)
class Seed:
    """One entrant's assigned position in the seeding order.

    `number` is **persisted** (A64-019.3 §4): a later phase must never
    recompute historical seeding from current ratings, because the ratings
    will have moved and the bracket would stop matching the one that was
    published.
    """

    player_id: UUID
    number: int
    rating: float
    deviation: float
    is_provisional: bool


@dataclass(frozen=True, slots=True)
class PlannedPairing:
    """One slot of the first round, before any match exists.

    `light_player_id` and `dark_player_id` are the two seats. Exactly one is
    `None` for a bye — a bye is an **empty slot**, never a fake player and
    never a match (§7).
    """

    round_number: int
    slot: int
    light_player_id: UUID | None
    dark_player_id: UUID | None
    light_seed: int | None
    dark_seed: int | None

    @property
    def is_bye(self) -> bool:
        return (self.light_player_id is None) != (self.dark_player_id is None)

    @property
    def present_player_id(self) -> UUID | None:
        """The single participant of a bye, or `None` when both seats are
        filled. What A64-019.4 advances without a match."""
        if not self.is_bye:
            return None
        return self.light_player_id or self.dark_player_id


def seeded(entrants: list[SeedInput]) -> list[Seed]:
    """Entrants in seed order, numbered from 1.

    Total by construction — see this module's docstring on why the third
    key matters more than it looks.
    """
    if len(entrants) < MIN_ENTRANTS:
        raise InvalidCapacity(
            f"a tournament needs at least {MIN_ENTRANTS} active entrants, got {len(entrants)}"
        )

    ordered = sorted(entrants, key=lambda e: (-e.rating, e.deviation, e.player_id.bytes))
    return [
        Seed(
            player_id=entrant.player_id,
            number=index,
            rating=entrant.rating,
            deviation=entrant.deviation,
            is_provisional=entrant.is_provisional,
        )
        for index, entrant in enumerate(ordered, start=FIRST_SEED)
    ]


def bracket_size(entrant_count: int) -> int:
    """The smallest power of two at or above `entrant_count` — §5.

    Sized from who turned up, never from `tournament.capacity`: capacity is
    the registration maximum, and a tournament with capacity 10 and 6
    entrants plays an 8-bracket rather than a 16-bracket with four extra
    byes nobody earned.
    """
    if entrant_count < MIN_ENTRANTS:
        raise InvalidCapacity(
            f"a bracket needs at least {MIN_ENTRANTS} entrants, got {entrant_count}"
        )

    size = 1
    while size < entrant_count:
        size *= 2
    return size


def seed_slots(size: int) -> list[int]:
    """Seed numbers in bracket-slot order — the placement algorithm.

    The recursive doubling in this module's docstring, written iteratively
    because the recursion is a fold and the loop is easier to check against
    the published orders (`[1,2]`, `[1,4,3,2]`, `[1,8,5,4,3,6,7,2]`).
    """
    order = [FIRST_SEED]
    while len(order) < size:
        doubled = len(order) * 2
        # Each existing seed keeps its slot; its new neighbour is the seed
        # that mirrors it, so the best draws the worst at every depth.
        order = [value for seed in order for value in (seed, doubled + 1 - seed)]
    return order


def first_round_pairings(seeds: list[Seed], *, round_number: int = 1) -> list[PlannedPairing]:
    """The first round's slots, from a seeded field.

    Deterministic end to end: the same entrants and the same ratings produce
    the same slots, the same sides and the same byes. That is what lets a
    retry return an identical plan rather than a different one (§11).
    """
    size = bracket_size(len(seeds))
    by_number = {seed.number: seed for seed in seeds}
    order = seed_slots(size)

    pairings: list[PlannedPairing] = []
    for slot, index in enumerate(range(0, size, 2)):
        high, low = by_number.get(order[index]), by_number.get(order[index + 1])

        # The higher seed takes the light seat on even slots and the dark
        # one on odd slots — deterministic, and alternating so moving first
        # is not always the better player's. See this module's docstring.
        #
        # Expressed as a boolean rather than `engine.PlayerSide`: R-2 lets
        # only `game`, `replay` and `fairplay` import the engine, and a
        # tournament decides *which seat*, not what a side means. The two
        # seats are already named by the fields below.
        higher_takes_light = slot % 2 == 0
        light, dark = (high, low) if higher_takes_light else (low, high)

        pairings.append(
            PlannedPairing(
                round_number=round_number,
                slot=slot,
                light_player_id=light.player_id if light else None,
                dark_player_id=dark.player_id if dark else None,
                light_seed=light.number if light else None,
                dark_seed=dark.number if dark else None,
            )
        )

    return pairings


__all__ = [
    "FIRST_SEED",
    "MIN_ENTRANTS",
    "PlannedPairing",
    "Seed",
    "SeedInput",
    "bracket_size",
    "first_round_pairings",
    "seed_slots",
    "seeded",
]
