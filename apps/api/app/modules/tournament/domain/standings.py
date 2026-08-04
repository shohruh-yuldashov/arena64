"""Final placement — SPEC-TOURNAMENT §6f, A64-019.6.

Pure. A finished bracket in, one `Standing` per entrant out: no clock, no
database, no ratings. That is what lets the placement rule be checked
against the published table rather than against whatever the repository
happened to return.

## Placement is the elimination round, and nothing else

Single elimination ranks by **how far you got**, so every player knocked out
in the same round finished in the same place:

    rank(champion)              1
    rank(eliminated in round r) 2 ** (rounds - r) + 1

    8 players, 3 rounds
        champion            1
        final loser         2       2**0 + 1
        semi-final losers   3, 3    2**1 + 1
        quarter losers      5,5,5,5 2**2 + 1

**Tied tiers are not broken.** Not by rating, not by seed, not by a head-to-
head that never happened, not by move count and not by duration. Two players
knocked out in the same round have exactly the same evidence about them —
they beat the same number of opponents and lost to somebody still in — and
inventing an order would publish a comparison nobody made. The seed stays
metadata: it explains the draw, it does not rank the result.

The gap in the numbers is the point, and is how every bracket sport reports
a draw sheet: two players share third, so nobody is fourth, and the next tier
starts at fifth.

## Statistics count games, never advancements

A win and a loss come from a `game` match somebody played. An advancement
that no game produced — a bye, two draws resolved by seed (§6c), a no-show
(§6e) — is counted as an **adjudicated advancement** and as nothing else.
Recording one as a win would put a competitive fact in a permanent record
that never happened, which is the same rule `AttemptOutcome.NO_SHOW` exists
to keep.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.modules.tournament.domain.attempts import (
    AdvancementReason,
    AttemptOutcome,
    PairingAttempt,
)
from app.modules.tournament.domain.bracket_plan import BracketSlot
from app.modules.tournament.domain.exceptions import InvalidBracketPosition

#: The champion's rank. An ordinal, like every other count on this platform.
FIRST_PLACE = 1

#: The runner-up's. Named because two constraints and one status refer to it.
SECOND_PLACE = 2


class FinalStatus(StrEnum):
    """How a participant's tournament ended.

    Every member exists from day one for R-19's reason: a status added after
    results have been recorded makes every historical query about it wrong
    and unfixable.
    """

    CHAMPION = "champion"
    RUNNER_UP = "runner_up"
    ELIMINATED = "eliminated"

    WITHDRAWN = "withdrawn"
    """Left before the field was fixed.

    **Nothing produces one in v0.x**, and that is a property of the
    lifecycle rather than an omission: a withdrawal is only permitted while
    registration is open (§4), so a withdrawn player is never seeded and
    never appears in a bracket. The member exists so that the day
    mid-tournament withdrawal is decided — it needs the Administration epic,
    OQ-1 — it is a use of this enum rather than a change to it.
    """


@dataclass(frozen=True, slots=True)
class Standing:
    """One entrant's final, immutable result.

    A value rather than a row: the completion service materialises these
    once and storage never recomputes them. What makes that safe is that
    every field here is derived from a bracket that can no longer change.
    """

    tournament_id: UUID
    player_id: UUID
    final_rank: int
    seed_number: int

    elimination_round: int | None
    """The round this player was knocked out in, or `None` for the champion."""

    eliminated_by_player_id: UUID | None
    """Who knocked them out. `None` exactly when `elimination_round` is."""

    wins: int
    losses: int
    draws: int
    """Games actually played. A bye, an adjudication and a no-show move none
    of these — see this module's docstring."""

    adjudicated_advancements: int
    """Rounds this player advanced without winning a game.

    A bye is **not** one: nobody adjudicated anything, the bracket simply
    had an empty seat. What counts is a decision the platform made in place
    of a result — §6c's two-draw seed tie-break and §6e's no-show.
    """

    final_status: FinalStatus

    created_at: datetime
    """When the result was materialised — the instant the tournament
    completed, from the injected clock. Not a database default: the whole
    set is one snapshot, so every row of it shares one instant rather than
    however many the flush happened to take."""


def rank_for(elimination_round: int, *, rounds: int) -> int:
    """Where a player eliminated in `elimination_round` finished.

    `2 ** (rounds - r) + 1`, which is the number of players still in the
    tournament when that round began, plus one. Every player knocked out
    then shares it.
    """
    if not 1 <= elimination_round <= rounds:
        raise InvalidBracketPosition(
            f"a {rounds}-round tournament has no round {elimination_round}"
        )
    return int(2 ** (rounds - elimination_round)) + 1


def standings_for(
    tournament_id: UUID,
    *,
    nodes: Sequence[BracketSlot],
    attempts: Sequence[PairingAttempt],
    seeds: dict[UUID, int],
    at: datetime,
) -> list[Standing]:
    """Every entrant's final result, from a decided bracket.

    Ordered by rank, then seed, then player id — the same total order the
    API publishes, computed here so storage and the wire cannot disagree
    about it.

    Raises `InvalidBracketPosition` unless the final has a winner: a
    tournament without one is not finished, and materialising standings for
    it would freeze a result that is still being played.
    """
    rounds = max((node.round_number for node in nodes), default=0)
    final = next((node for node in nodes if node.round_number == rounds), None)
    if final is None or final.winner_id is None:
        raise InvalidBracketPosition("a tournament is not complete until its final has a winner")

    played = _statistics(attempts)
    adjudicated = _adjudicated(nodes)
    eliminations = _eliminations(nodes)

    standings = [
        _standing(
            tournament_id,
            player_id=player_id,
            seed_number=seed_number,
            champion=player_id == final.winner_id,
            rounds=rounds,
            eliminations=eliminations,
            played=played,
            adjudicated=adjudicated,
            at=at,
        )
        for player_id, seed_number in seeds.items()
    ]
    return sorted(standings, key=lambda s: (s.final_rank, s.seed_number, s.player_id.bytes))


def _standing(
    tournament_id: UUID,
    *,
    player_id: UUID,
    seed_number: int,
    champion: bool,
    rounds: int,
    eliminations: dict[UUID, tuple[int, UUID]],
    played: dict[UUID, "_Record"],
    adjudicated: dict[UUID, int],
    at: datetime,
) -> Standing:
    record = played.get(player_id, _Record())
    elimination = eliminations.get(player_id)

    if champion:
        rank, elimination_round, eliminated_by = FIRST_PLACE, None, None
    elif elimination is not None:
        elimination_round, eliminated_by = elimination
        rank = rank_for(elimination_round, rounds=rounds)
    else:  # pragma: no cover — a non-champion in a decided bracket lost a node
        raise InvalidBracketPosition(f"player {player_id} neither won nor was eliminated")

    return Standing(
        tournament_id=tournament_id,
        player_id=player_id,
        final_rank=rank,
        seed_number=seed_number,
        elimination_round=elimination_round,
        eliminated_by_player_id=eliminated_by,
        wins=record.wins,
        losses=record.losses,
        draws=record.draws,
        adjudicated_advancements=adjudicated.get(player_id, 0),
        final_status=_status_for(rank),
        created_at=at,
    )


def _status_for(rank: int) -> FinalStatus:
    if rank == FIRST_PLACE:
        return FinalStatus.CHAMPION
    if rank == SECOND_PLACE:
        return FinalStatus.RUNNER_UP
    return FinalStatus.ELIMINATED


@dataclass(slots=True)
class _Record:
    """One player's games. Mutable, and scoped to a single derivation."""

    wins: int = 0
    losses: int = 0
    draws: int = 0


def _statistics(attempts: Sequence[PairingAttempt]) -> dict[UUID, _Record]:
    """Wins, losses and draws from the matches that were actually played.

    Only `DECISIVE` and `DRAW` attempts count. `NO_SHOW` is deliberately
    absent: nobody played it, so it produces no win for the advancing player
    and no loss for the absent one — §7's "no fake win/loss is created for an
    adjudicated advancement".
    """
    records: dict[UUID, _Record] = {}

    for attempt in attempts:
        light = records.setdefault(attempt.light_player_id, _Record())
        dark = records.setdefault(attempt.dark_player_id, _Record())

        if attempt.outcome is AttemptOutcome.DRAW:
            light.draws += 1
            dark.draws += 1
        elif attempt.outcome is AttemptOutcome.DECISIVE and attempt.winner_id is not None:
            winner, loser = (
                (light, dark) if attempt.winner_id == attempt.light_player_id else (dark, light)
            )
            winner.wins += 1
            loser.losses += 1

    return records


def _adjudicated(nodes: Sequence[BracketSlot]) -> dict[UUID, int]:
    """How many rounds each player advanced by a decision rather than a game.

    `ADJUDICATION` only. A `BYE` is excluded because nothing was
    adjudicated — the bracket had an empty seat, which is arithmetic rather
    than a ruling, and §7 requires it to move no counter.
    """
    counts: dict[UUID, int] = {}
    for node in nodes:
        if node.advancement_reason is AdvancementReason.ADJUDICATION and node.winner_id:
            counts[node.winner_id] = counts.get(node.winner_id, 0) + 1
    return counts


def _eliminations(nodes: Sequence[BracketSlot]) -> dict[UUID, tuple[int, UUID]]:
    """Each player's `(round, conqueror)`, from the node they lost.

    A node with a winner eliminates its *other* participant, whatever
    decided it — a game, two draws, or an absence. A node with one
    participant eliminates nobody, which is what a bye is.
    """
    eliminations: dict[UUID, tuple[int, UUID]] = {}
    for node in nodes:
        if node.winner_id is None:
            continue
        for player_id in node.participants:
            if player_id != node.winner_id:
                eliminations[player_id] = (node.round_number, node.winner_id)
    return eliminations


__all__ = [
    "FIRST_PLACE",
    "SECOND_PLACE",
    "FinalStatus",
    "Standing",
    "rank_for",
    "standings_for",
]
