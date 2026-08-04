"""Materialising a whole bracket, and moving winners through it. §1, §6, §7.

Pure. The tree is built from seed numbers alone — no ratings, no clock, no
database — which is what lets a retry produce an identical bracket and a
test check bye propagation without a transaction.

## The tree is built whole, once

    round 1   size/2 nodes, participants known
    round 2   size/4 nodes, slots empty
    ...
    round R   1 node, the final

Later rounds are materialised **empty** rather than created when the
previous one finishes. §1's reason is immutability: a bracket generated
lazily from current results is a bracket that can differ from the one
players read, and "who could I meet in the semi-final" stops being
answerable in advance.

The cost is `size - 1` rows written once. The benefit is that placement is
never recomputed.

## Coordinates, and the parent rule

A node is `(round_number, slot)`. Its parent is `(round_number + 1, slot //
2)` and its two children are `(round_number - 1, slot * 2)` and
`(… , slot * 2 + 1)`. Round 1 is the widest, round `R` is the final — which
is the direction a reader thinks in, and the inverse of `BracketNode`'s
depth-from-the-final.

Storing parent and child ids would be columns that can disagree with the
coordinates. Arithmetic cannot.

## Bye propagation is a fixed point, not a special case

A node with exactly one participant has a winner without a match. Filling
the parent may leave *it* with one participant, so propagation repeats
until nothing more can be decided:

    one player   advances, and may create another bye above
    two players  stops — this needs a real match
    no players   stops — nothing to decide yet

`propagated` runs to that fixed point and is idempotent: applied to its own
output it changes nothing, because every node it could decide is already
decided.
"""

from dataclasses import dataclass, replace
from typing import Final
from uuid import UUID

from app.modules.tournament.domain.exceptions import (
    InvalidBracketPosition,
    InvalidRoundNumber,
)
from app.modules.tournament.domain.seeding import PlannedPairing, bracket_size

FIRST_ROUND: Final = 1


@dataclass(frozen=True, slots=True)
class PersistedSeed:
    """A seed as storage holds it — §4.

    **Only what is actually stored.** The rating and deviation that produced
    the seed are deliberately absent: the seed number *is* the persisted
    answer, and returning a fabricated `0.0` beside it would be a value that
    reads like a measurement and is not.

    That was a real defect in A64-019.3's `seeds_for`, which returned
    `rating=0.0, deviation=0.0, is_provisional=False` for every row. A
    caller that trusted those numbers would have reseeded a tournament to
    all-equal.
    """

    tournament_id: UUID
    player_id: UUID
    seed_number: int


@dataclass(frozen=True, slots=True)
class BracketSlot:
    """One node of the materialised tree.

    Distinct from `seeding.PlannedPairing`, which is a *proposal* for round
    one: this is the durable node, and it carries the two things a proposal
    cannot — who won, and which match decided it.
    """

    round_number: int
    slot: int

    light_player_id: UUID | None = None
    dark_player_id: UUID | None = None
    light_seed: int | None = None
    dark_seed: int | None = None

    winner_id: UUID | None = None
    match_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.round_number < FIRST_ROUND:
            raise InvalidRoundNumber(
                f"round numbers start at {FIRST_ROUND}, got {self.round_number}"
            )
        if self.slot < 0:
            raise InvalidBracketPosition(f"slot cannot be negative, got {self.slot}")

    @property
    def participants(self) -> tuple[UUID, ...]:
        return tuple(
            player for player in (self.light_player_id, self.dark_player_id) if player is not None
        )

    @property
    def is_bye(self) -> bool:
        """Exactly one participant — decidable without a match."""
        return len(self.participants) == 1 and self.winner_id is None

    @property
    def needs_a_match(self) -> bool:
        """Two participants and no winner — A64-019.5 creates the match."""
        return len(self.participants) == 2 and self.winner_id is None

    def parent(self) -> tuple[int, int]:
        """Where this node's winner goes.

        Always a coordinate, never `None` — the *final*'s parent is simply a
        coordinate the bracket does not contain, and a caller discovers that
        by looking it up rather than by knowing the tree's depth. One rule
        for every node beats a special case at the top.
        """
        return (self.round_number + 1, self.slot // 2)

    def takes_light_seat_of_parent(self) -> bool:
        """Whether this node's winner fills the parent's light seat.

        Even slots feed the light seat, odd ones the dark — the same
        arithmetic that makes `slot // 2` the parent, seen from the other
        side. Deterministic, so an advancement lands in the same seat on a
        retry.
        """
        return self.slot % 2 == 0

    def with_winner(self, player_id: UUID) -> "BracketSlot":
        """This node decided. Raises unless the winner played in it.

        The one bracket error nothing downstream detects: an advancement by
        somebody who was never here produces a final between players who
        never met, visible only after the rounds beneath it are recorded.
        """
        if player_id not in self.participants:
            raise InvalidBracketPosition("the winner of a node must be one of its participants")
        return replace(self, winner_id=player_id)

    def with_participant(self, player_id: UUID, *, seed: int | None, light: bool) -> "BracketSlot":
        """This node with one seat filled by an advancing winner.

        The seed travels with the player. Not decoration: it is how a
        bracket explains *why* a node looks the way it does, and the
        relation's own check constraint pairs the two columns — a seat with
        a player and no seed is a row the database refuses.
        """
        if light:
            return replace(self, light_player_id=player_id, light_seed=seed)
        return replace(self, dark_player_id=player_id, dark_seed=seed)

    def seed_of(self, player_id: UUID) -> int | None:
        """This node's seed for one of its participants."""
        if player_id == self.light_player_id:
            return self.light_seed
        if player_id == self.dark_player_id:
            return self.dark_seed
        return None


def round_count(size: int) -> int:
    """How many rounds a bracket of `size` has. `8 -> 3`."""
    rounds = 0
    while 2**rounds < size:
        rounds += 1
    return rounds


def materialised(
    seeds: list[PersistedSeed], first_round: list[PlannedPairing]
) -> list[BracketSlot]:
    """The complete tree — round one filled, later rounds empty. §1, §5.

    Built whole rather than lazily, so placement is never recomputed and a
    published bracket cannot shift. `size - 1` nodes, written once.
    """
    size = bracket_size(len(seeds))
    nodes = [
        BracketSlot(
            round_number=pairing.round_number,
            slot=pairing.slot,
            light_player_id=pairing.light_player_id,
            dark_player_id=pairing.dark_player_id,
            light_seed=pairing.light_seed,
            dark_seed=pairing.dark_seed,
        )
        for pairing in first_round
    ]

    for round_number in range(FIRST_ROUND + 1, round_count(size) + 1):
        width = size // (2**round_number)
        nodes.extend(BracketSlot(round_number=round_number, slot=slot) for slot in range(width))

    return nodes


def propagated(nodes: list[BracketSlot]) -> list[BracketSlot]:
    """Every bye resolved, to a fixed point. §6.

    Idempotent by construction: applied to its own output nothing changes,
    because every node it could decide already is. That is what makes it
    safe to run on a retry and after every advancement.

    A node with **two** empty seats stops the chain — there is nothing to
    decide and inventing a winner would be a phantom advancement. A node
    with two players stops it too: that is a real match, which A64-019.5
    creates.
    """
    by_coordinate = {(node.round_number, node.slot): node for node in nodes}

    # Round by round, ascending, because a bye only ever moves a player
    # *upward*: by the time a round is reached, every node beneath it is
    # final, so one pass reaches the fixed point and a `while` loop would be
    # the same work with a termination condition to get wrong.
    for round_number in sorted({node.round_number for node in nodes}):
        for coordinate in sorted(c for c in by_coordinate if c[0] == round_number):
            node = by_coordinate[coordinate]
            if not node.is_bye or not _children_settled(by_coordinate, node):
                continue

            winner = node.participants[0]
            decided = node.with_winner(winner)
            by_coordinate[coordinate] = decided

            parent = by_coordinate.get(decided.parent())
            if parent is None:
                continue  # the final: its winner wins the tournament
            by_coordinate[decided.parent()] = parent.with_participant(
                winner,
                seed=decided.seed_of(winner),
                light=decided.takes_light_seat_of_parent(),
            )

    return [by_coordinate[key] for key in sorted(by_coordinate)]


def _children_settled(nodes: dict[tuple[int, int], "BracketSlot"], node: "BracketSlot") -> bool:
    """Whether nothing beneath `node` can still deliver it a participant.

    **The rule that stops a bye deciding a node too early.** A semi-final
    holding one player because the other semi has not been played is *not* a
    bye — it is waiting. Without this check the propagation would hand it a
    winner and the bracket would skip a match that has to happen.

    A round-one node has no children and is settled by definition. Above
    that, a child is settled when it has a winner or can never produce one:
    an empty subtree delivers nobody, which is how a bracket with many byes
    still resolves rather than stalling.
    """
    if node.round_number == FIRST_ROUND:
        return True

    children = ((node.round_number - 1, node.slot * 2), (node.round_number - 1, node.slot * 2 + 1))
    for coordinate in children:
        child = nodes.get(coordinate)
        if child is None:
            continue
        if child.winner_id is None and child.participants:
            return False
    return True


def bracket_for(seeds: list[PersistedSeed], first_round: list[PlannedPairing]) -> list[BracketSlot]:
    """The whole tree with its initial byes already resolved.

    One function so a caller cannot materialise without propagating — a
    bracket with unresolved byes would have round-one nodes waiting for
    matches that must never exist.
    """
    return propagated(materialised(seeds, first_round))


__all__ = [
    "FIRST_ROUND",
    "BracketSlot",
    "PersistedSeed",
    "bracket_for",
    "materialised",
    "propagated",
    "round_count",
]
