"""Wire shapes for a tournament's public reads — A64-019.6 §9–§12.

Pydantic models built from `application/read_models`, never from ORM rows.
No internal identifier, no column name and no exception type reaches a
client.

## The cursor is opaque

A client receives `next_cursor` as one string and sends it back unread. Its
contents are `(registered_at, tournament_id)` — an implementation detail of
the ordering — and publishing them as fields would make the ordering a
contract that cannot change without breaking clients. The same choice
`game`'s match history makes, and the same reason.

Encoded, not encrypted: it carries nothing a caller could not see in the
page it came from.
"""

import base64
import binascii
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.error_codes import ErrorCode
from app.core.exceptions import ValidationError
from app.modules.tournament.application.read_models import (
    AttemptSummary,
    BracketNodeView,
    BracketView,
    HistoryCursor,
    PlayerTournamentPage,
    RoundView,
    StandingView,
    TournamentSummary,
)

_CURSOR_SEPARATOR = "|"


class InvalidCursor(ValidationError):
    """The `after` parameter was not a cursor this API issued."""

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_CURSOR


def encode_cursor(cursor: HistoryCursor) -> str:
    raw = f"{cursor.registered_at.isoformat()}{_CURSOR_SEPARATOR}{cursor.tournament_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(value: str) -> HistoryCursor:
    """A cursor string as the read model's value. Raises `InvalidCursor`.

    Every failure mode collapses to one error: bad base64, a missing
    separator, an unparseable instant or id. A client cannot act differently
    on any of them — the answer is always "ask for the first page" — and
    distinguishing them would describe the encoding to whoever is probing it.
    """
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        instant, _, tournament_id = raw.partition(_CURSOR_SEPARATOR)
        return HistoryCursor(
            registered_at=datetime.fromisoformat(instant),
            tournament_id=UUID(tournament_id),
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as malformed:
        raise InvalidCursor("the pagination cursor is not valid") from malformed


class TournamentResponse(BaseModel):
    """One tournament's public detail — §9.

    `created_by` is deliberately absent: who opened a tournament is
    operational, and publishing it would leak which tournaments the platform
    ran itself.
    """

    id: UUID
    name: str
    format: str
    variant: str
    speed_class: str
    rated: bool
    capacity: int
    status: str
    entrant_count: int = Field(description="Live registrations.")
    current_round: int | None = Field(default=None, description="The round being played, or null.")
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def of(cls, summary: TournamentSummary) -> "TournamentResponse":
        return cls(
            id=summary.id,
            name=summary.name,
            format=summary.format.value,
            variant=summary.variant,
            speed_class=summary.speed_class,
            rated=summary.rated,
            capacity=summary.capacity,
            status=summary.status.value,
            entrant_count=summary.entrant_count,
            current_round=summary.current_round,
            created_at=summary.created_at,
            started_at=summary.started_at,
            completed_at=summary.completed_at,
        )


class AttemptResponse(BaseModel):
    """One `game` match played for a node — §10.

    `match_id` is published so a reader can follow a node to the game that
    decided it. The moves are not: a replay is `game`'s to serve, on its own
    endpoint and under its own visibility rule.
    """

    attempt_number: int
    match_id: UUID
    light_player_id: UUID
    dark_player_id: UUID
    status: str
    outcome: str | None = None
    winner_id: UUID | None = None

    @classmethod
    def of(cls, attempt: AttemptSummary) -> "AttemptResponse":
        return cls(
            attempt_number=attempt.attempt_number,
            match_id=attempt.match_id,
            light_player_id=attempt.light_player_id,
            dark_player_id=attempt.dark_player_id,
            status=attempt.status.value,
            outcome=attempt.outcome.value if attempt.outcome else None,
            winner_id=attempt.winner_id,
        )


class BracketNodeResponse(BaseModel):
    """One node of the published bracket — §10."""

    pairing_id: UUID
    round_number: int
    slot: int
    light_player_id: UUID | None = None
    dark_player_id: UUID | None = None
    light_seed: int | None = None
    dark_seed: int | None = None
    winner_id: UUID | None = None
    advancement_reason: str | None = Field(default=None, description="played, bye or adjudication.")
    is_bye: bool
    attempts: list[AttemptResponse]

    @classmethod
    def of(cls, node: BracketNodeView) -> "BracketNodeResponse":
        return cls(
            pairing_id=node.pairing_id,
            round_number=node.round_number,
            slot=node.slot,
            light_player_id=node.light_player_id,
            dark_player_id=node.dark_player_id,
            light_seed=node.light_seed,
            dark_seed=node.dark_seed,
            winner_id=node.winner_id,
            advancement_reason=(node.advancement_reason.value if node.advancement_reason else None),
            is_bye=node.is_bye,
            attempts=[AttemptResponse.of(attempt) for attempt in node.attempts],
        )


class RoundResponse(BaseModel):
    round_number: int
    status: str
    nodes: list[BracketNodeResponse]

    @classmethod
    def of(cls, round_: RoundView) -> "RoundResponse":
        return cls(
            round_number=round_.round_number,
            status=round_.status.value,
            nodes=[BracketNodeResponse.of(node) for node in round_.nodes],
        )


class BracketResponse(BaseModel):
    tournament_id: UUID
    rounds: list[RoundResponse]

    @classmethod
    def of(cls, bracket: BracketView) -> "BracketResponse":
        return cls(
            tournament_id=bracket.tournament_id,
            rounds=[RoundResponse.of(round_) for round_ in bracket.rounds],
        )


class StandingResponse(BaseModel):
    """One entrant's final result — §11.

    Ranks are **not dense**: two players knocked out in the same round share
    one, so an eight-player bracket has no fourth place. That is the
    placement rule rather than a gap, and a client that renumbered would be
    publishing a comparison nobody made.
    """

    player_id: UUID
    final_rank: int
    seed_number: int
    wins: int
    losses: int
    draws: int
    adjudicated_advancements: int = Field(
        description="Rounds advanced without a game — a seed tie-break or a no-show."
    )
    final_status: str
    elimination_round: int | None = None
    eliminated_by_player_id: UUID | None = None

    @classmethod
    def of(cls, standing: StandingView) -> "StandingResponse":
        return cls(
            player_id=standing.player_id,
            final_rank=standing.final_rank,
            seed_number=standing.seed_number,
            wins=standing.wins,
            losses=standing.losses,
            draws=standing.draws,
            adjudicated_advancements=standing.adjudicated_advancements,
            final_status=standing.final_status.value,
            elimination_round=standing.elimination_round,
            eliminated_by_player_id=standing.eliminated_by_player_id,
        )


class StandingsResponse(BaseModel):
    """A completed tournament's placement, in published order.

    Empty while the tournament is still being played — standings are
    materialised at completion (§6f), and nothing derives a partial one.
    """

    tournament_id: UUID
    standings: list[StandingResponse]

    @classmethod
    def of(cls, tournament_id: UUID, standings: list[StandingView]) -> "StandingsResponse":
        return cls(
            tournament_id=tournament_id,
            standings=[StandingResponse.of(standing) for standing in standings],
        )


class PlayerTournamentResponse(BaseModel):
    """One tournament a player entered — §12."""

    tournament: TournamentResponse
    seed_number: int | None = None
    final_rank: int | None = None
    final_status: str | None = None


class PlayerTournamentsResponse(BaseModel):
    """One page of a player's tournament history, newest first."""

    entries: list[PlayerTournamentResponse]
    next_cursor: str | None = Field(
        default=None, description="Opaque; send it back unread for the next page."
    )

    @classmethod
    def of(cls, page: PlayerTournamentPage) -> "PlayerTournamentsResponse":
        return cls(
            entries=[
                PlayerTournamentResponse(
                    tournament=TournamentResponse.of(entry.tournament),
                    seed_number=entry.seed_number,
                    final_rank=entry.final_rank,
                    final_status=entry.final_status.value if entry.final_status else None,
                )
                for entry in page.entries
            ],
            next_cursor=encode_cursor(page.next_cursor) if page.next_cursor else None,
        )


__all__ = [
    "AttemptResponse",
    "BracketNodeResponse",
    "BracketResponse",
    "InvalidCursor",
    "PlayerTournamentResponse",
    "PlayerTournamentsResponse",
    "RoundResponse",
    "StandingResponse",
    "StandingsResponse",
    "TournamentResponse",
    "decode_cursor",
    "encode_cursor",
]
