"""Turning a bracket node into a `game` match — SPEC-TOURNAMENT §5, §6c.

The one place `tournament` crosses into `game`, and it is a **command on a
published port** (`game.public.MatchCreationUseCase`) — the same edge
`matchmaking` uses. R-3 permits exactly this and nothing more: this module
asks for a match to exist and can neither advance one nor read one back.

Three callers, one method, and that is deliberate. Starting a tournament,
launching a rematch after a draw, and repairing a node the reconciler found
without a match are the same operation with different inputs — a second
copy of it would be a second place the origin, the seats or the idempotency
key could be got wrong.

## Idempotency is `game`'s key, derived rather than stored

`CreateMatchRequest.pairing_id` is `attempts.match_key(pairing, number)`:
derived from the node and the attempt, so a retry computes the same value
and `game`'s unique index returns the match the first call created with
`created=False`. That is what makes "create the match, then record the
attempt" safe to interrupt — the recovery is to ask again, not to guess.

The order matters and is the opposite of what it looks like it should be.
The match is created **first**, because a match with no attempt row is
recoverable (ask `game` again, write the row) and an attempt row naming a
match that was never created is not.

## Ratings are read in one batch

§3's rule, and the reason is the seeding service's: a per-player read
spreads one instant across the field. Here it also removes an N+1 from the
moment a 128-player tournament starts, which is the one moment this module
does the most work at once.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.modules.game.public import (
    AcceptancePolicy,
    CreateMatchRequest,
    MatchCreationUseCase,
    MatchOrigin,
    MatchParticipant,
    SeatRating,
    game_engine_version,
)
from app.modules.rating.public import RatingSnapshot, SpeedClass
from app.modules.tournament.application.ports import (
    AttemptAlreadyExists,
    PairingAttemptRepository,
    RatingSnapshots,
)
from app.modules.tournament.domain.attempts import (
    FIRST_ATTEMPT,
    AttemptStatus,
    PairingAttempt,
    match_key,
)
from app.modules.tournament.domain.bracket_plan import BracketSlot
from app.modules.tournament.domain.tournament import Tournament

logger = logging.getLogger(__name__)

#: The acceptance window a tournament match nominally carries.
#:
#: **Nothing uses it.** A64-019.5H makes these matches system-activated
#: (`AcceptancePolicy.SYSTEM`): they are created already `ACTIVE`, so there
#: is no handshake to miss and the expiry sweep never claims one. The field
#: is required by `CreateMatchRequest` and `MatchRecord` enforces only that
#: it is after `created_at`, so it is set to the no-show window for the one
#: reason a reader would want — the two numbers describing "how long this
#: match waits for its players" should not disagree.
#:
#: What actually ends an unattended tournament match is §6e's no-show
#: deadline, which is a *tournament* policy and is configured as one.
ACCEPTANCE_WINDOW_SECONDS: Final = 5 * 60


@dataclass(frozen=True, slots=True)
class PlannedAttempt:
    """One match this module has been asked to bring into existence.

    Carries the seats **explicitly** rather than deriving them from the
    node, because a rematch swaps them (§6c) and a launcher that read the
    pairing would give the same player the first move in both games of a
    tie.
    """

    pairing_id: UUID
    attempt_number: int
    light_player_id: UUID
    dark_player_id: UUID

    def players(self) -> tuple[UUID, UUID]:
        return (self.light_player_id, self.dark_player_id)


def plans_for(nodes: Sequence[BracketSlot]) -> list[PlannedAttempt]:
    """First attempts for every one of these nodes that needs a match.

    The filter is `needs_a_match` — two participants and no winner — rather
    than anything about rounds, so a **bye is skipped without a rule to
    apply**: it has one participant, so it never qualifies. That is §6a's
    "a bye is an empty slot, never a match" expressed as the absence of a
    special case.

    A node the domain has computed but storage has not seen has no id and is
    not launchable; it is dropped here rather than defaulted, because a
    fabricated reference would be a match `game` hands back to nobody.
    """
    return [
        PlannedAttempt(
            pairing_id=pairing_id,
            attempt_number=FIRST_ATTEMPT,
            # `needs_a_match` is exactly "both seats filled", so the pair is
            # (light, dark) and indexing cannot pick up an empty seat.
            light_player_id=node.participants[0],
            dark_player_id=node.participants[1],
        )
        for node in nodes
        if node.needs_a_match and (pairing_id := node.id) is not None
    ]


class TournamentMatchLauncher:
    """Creates `game` matches for bracket nodes and records the attempts."""

    def __init__(
        self,
        *,
        matches: MatchCreationUseCase,
        ratings: RatingSnapshots,
        attempts: PairingAttemptRepository,
        clock: Clock,
        no_show_seconds: int,
    ) -> None:
        self._matches = matches
        self._ratings = ratings
        self._attempts = attempts
        self._clock = clock
        self._no_show_seconds = no_show_seconds

    async def launch(
        self, tournament: Tournament, planned: Sequence[PlannedAttempt]
    ) -> list[PairingAttempt]:
        """Creates a match per plan and records each attempt. Idempotent.

        Enlists in the caller's transaction and never commits: the attempt
        rows and whatever lifecycle change prompted them are one fact.

        A plan whose attempt row already exists is **not** an error — it is
        a retry, and the stored row is returned. What would be an error is
        creating a second match for it, which `game`'s own key makes
        impossible.
        """
        if not planned:
            return []

        snapshots = await self._snapshots(tournament, planned)
        # **Computed once per launch, from the injected clock.** Every match
        # a round starts waits the same window from the same instant, so two
        # players in the same round are never given different deadlines
        # because one node happened to be created a second later.
        no_show_deadline = self._clock.now() + timedelta(seconds=self._no_show_seconds)

        recorded: list[PairingAttempt] = []
        for plan in planned:
            recorded.append(await self._launch_one(tournament, plan, snapshots, no_show_deadline))
        return recorded

    async def _launch_one(
        self,
        tournament: Tournament,
        plan: PlannedAttempt,
        snapshots: Mapping[UUID, RatingSnapshot],
        no_show_deadline: datetime,
    ) -> PairingAttempt:
        result = await self._matches.create_match(
            CreateMatchRequest(
                pairing_id=match_key(plan.pairing_id, plan.attempt_number),
                variant=tournament.variant,
                rated=tournament.rated,
                engine_version=game_engine_version(),
                acceptance_deadline=no_show_deadline,
                # **System-activated** — A64-019.5H, §6e. Nobody is asked to
                # accept a fixture they entered a tournament to play, so the
                # match is created already `ACTIVE` and the players may join
                # it through the live gateway immediately. What replaces the
                # handshake is the no-show deadline below.
                acceptance=AcceptancePolicy.SYSTEM,
                light=MatchParticipant(
                    player_id=plan.light_player_id,
                    # **No ticket, and that is the fact.** A tournament
                    # entrant did not arrive through a queue. A64-019.5
                    # derived a uuid5 here to satisfy a `NOT NULL` and so
                    # recorded a ticket that never existed; A64-019.5H made
                    # the column nullable instead — see `specs/tournament.md`
                    # §6c, which recorded the wart before it was fixed.
                    queue_ticket_id=None,
                    rating=_seat(snapshots[plan.light_player_id], tournament.speed_class),
                ),
                dark=MatchParticipant(
                    player_id=plan.dark_player_id,
                    queue_ticket_id=None,
                    rating=_seat(snapshots[plan.dark_player_id], tournament.speed_class),
                ),
                # R-25's round trip. **The node's own id**, never an
                # encoding of the tournament, round and slot — §6c: an
                # opaque reference leaves this module's coordinates free to
                # be an implementation detail.
                origin=MatchOrigin.TOURNAMENT,
                origin_ref=plan.pairing_id,
            )
        )

        attempt = PairingAttempt(
            id=uuid4(),
            pairing_id=plan.pairing_id,
            attempt_number=plan.attempt_number,
            match_id=result.match_id,
            light_player_id=plan.light_player_id,
            dark_player_id=plan.dark_player_id,
            status=AttemptStatus.CREATED,
            # Stored on the attempt rather than recomputed at adjudication
            # time, so a deploy that changes the setting cannot move a
            # deadline a player was already given — §6e.
            no_show_deadline=no_show_deadline,
        )

        try:
            return await self._attempts.record(attempt)
        except AttemptAlreadyExists:
            # A retry, or a second worker. The row that won names the same
            # match — `game`'s key guaranteed it above — so re-reading is
            # reading this call's own answer, not accepting somebody else's.
            stored = await self._attempts.by_match(result.match_id)
            if stored is None:  # pragma: no cover — the constraint that
                # refused the insert is the one that says a row exists.
                raise
            return stored

    async def _snapshots(
        self, tournament: Tournament, planned: Sequence[PlannedAttempt]
    ) -> Mapping[UUID, RatingSnapshot]:
        """Every seat's rating, in one read — §3.

        `rating.public` fills an unrated player with the starting triple
        rather than omitting them, so indexing the result below cannot drop
        a seat.
        """
        players = {player for plan in planned for player in plan.players()}
        return await self._ratings.ratings_for(
            sorted(players),
            variant=tournament.variant,
            speed_class=tournament.speed_class,
        )


def _seat(snapshot: RatingSnapshot, speed_class: SpeedClass) -> SeatRating:
    """The seat snapshot `game` stores and hands back on completion.

    Copied into `game`'s own type rather than passed through, because
    `game` must not depend on `rating` (R-4) — see
    `game.public.matches.SeatRating`. What travels is five numbers and a
    string.
    """
    return SeatRating(
        value=snapshot.value,
        deviation=snapshot.deviation,
        volatility=snapshot.volatility,
        games_played=snapshot.games_played,
        is_provisional=snapshot.is_provisional,
        speed_class=speed_class.value,
    )


__all__ = [
    "ACCEPTANCE_WINDOW_SECONDS",
    "PlannedAttempt",
    "TournamentMatchLauncher",
    "plans_for",
]
