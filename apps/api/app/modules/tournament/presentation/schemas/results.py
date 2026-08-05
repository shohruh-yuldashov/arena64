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
from collections.abc import Iterable, Mapping
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
    TournamentListCursor,
    TournamentPage,
    TournamentSummary,
)
from app.modules.users.public import PublicUserProfile

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


def encode_list_cursor(cursor: TournamentListCursor) -> str:
    raw = f"{cursor.created_at.isoformat()}{_CURSOR_SEPARATOR}{cursor.tournament_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_list_cursor(value: str) -> TournamentListCursor:
    """A lobby cursor string as the read model's value. Raises
    `InvalidCursor`.

    A separate codec from `decode_cursor` rather than a shared generic one:
    the two orderings are over different instants, and a cursor issued by
    one endpoint accepted by the other would page a lobby by a registration
    time that nothing there stores. Same failure policy — every malformed
    variant is one `invalid_cursor`.
    """
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        instant, _, tournament_id = raw.partition(_CURSOR_SEPARATOR)
        return TournamentListCursor(
            created_at=datetime.fromisoformat(instant),
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
    registration_deadline: datetime | None = Field(
        default=None, description="When entries close on their own, or null for operator-closed."
    )
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
            registration_deadline=summary.registration_deadline,
            created_at=summary.created_at,
            started_at=summary.started_at,
            completed_at=summary.completed_at,
        )


class TournamentListResponse(BaseModel):
    """One page of the lobby, newest first — A64-020.0B.

    Entries are `TournamentResponse`, the **same** shape the detail endpoint
    returns, rather than a slimmer list-only variant. One mapper means a
    field cannot be published on one surface and withheld on the other, and
    a client can render a lobby card and a detail page from one type.
    """

    entries: list[TournamentResponse]
    next_cursor: str | None = Field(
        default=None, description="Opaque; send it back unread for the next page."
    )

    @classmethod
    def of(cls, page: TournamentPage) -> "TournamentListResponse":
        return cls(
            entries=[TournamentResponse.of(summary) for summary in page.entries],
            next_cursor=encode_list_cursor(page.next_cursor) if page.next_cursor else None,
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


class TournamentParticipantResponse(BaseModel):
    """Who a player id refers to — A64-020.6 §26.

    Composed from `users`' **public** profile read, gated by the same
    privacy policy every other surface uses. `username` and `display_name`
    are `None` for a deactivated account, which is the answer this platform
    gives everywhere rather than a special case here.

    No rating: a bracket seat carries a **seed**, which is the ordering the
    tournament was drawn on, and publishing a live rating beside it would
    invite reading one as the other. A replay seat carries the snapshot the
    match was played at (PR-3) and is the surface where a rating belongs.
    """

    player_id: UUID
    username: str | None = None
    display_name: str | None = None
    avatar_thumbnail_url: str | None = None

    @classmethod
    def of(
        cls, player_id: UUID, profile: PublicUserProfile | None
    ) -> "TournamentParticipantResponse":
        return cls(
            player_id=player_id,
            username=profile.username if profile else None,
            display_name=profile.display_name if profile else None,
            avatar_thumbnail_url=getattr(profile, "avatar_thumbnail_url", None),
        )


def _participants_of(
    player_ids: Iterable[UUID], profiles: Mapping[UUID, PublicUserProfile]
) -> list[TournamentParticipantResponse]:
    """The identity list both bracket and standings carry.

    Sorted, so two reads of an unchanged tournament produce byte-identical
    responses — a dictionary's iteration order is stable within a process
    and says nothing across two.
    """
    return [
        TournamentParticipantResponse.of(player_id, profiles.get(player_id))
        for player_id in sorted(player_ids, key=str)
    ]


class BracketResponse(BaseModel):
    """Every round and node, plus who the ids in them are — §10, §26.

    ## Why `participants` is a side list rather than an embedded object

    A player appears in one node per round they survive, so embedding their
    identity would repeat a champion's name `log2(field)` times and make the
    response grow with the bracket's *depth* rather than its width. A client
    joins on `player_id`, which it already holds from `winner_id`,
    `light_player_id` and `dark_player_id`.

    ## Why it is composed here at all

    Without it a client turns a seat into a name by asking, and a 128-player
    bracket is 127 nodes and 128 lookups — the N+1 A64-020.6 §26 forbids and
    the same one A64-020.5F's history prerequisite existed to prevent. One
    batched read, deduplicated, on the server side of the boundary.
    """

    tournament_id: UUID
    rounds: list[RoundResponse]
    participants: list[TournamentParticipantResponse] = Field(
        default_factory=list,
        description="Every player id appearing in the bracket, resolved to a public identity.",
    )

    @staticmethod
    def participant_ids_in(bracket: BracketView) -> list[UUID]:
        """Every distinct player the bracket names, in one pass.

        Winners are included as well as seats: a node whose child advanced
        by bye has a `winner_id` that is also a seat, and one that does not
        would otherwise render an unresolved id.
        """
        seen = {
            player_id
            for round_ in bracket.rounds
            for node in round_.nodes
            for player_id in (node.light_player_id, node.dark_player_id, node.winner_id)
            if player_id is not None
        }
        return sorted(seen, key=str)

    @classmethod
    def of(
        cls, bracket: BracketView, profiles: Mapping[UUID, PublicUserProfile] | None = None
    ) -> "BracketResponse":
        return cls(
            tournament_id=bracket.tournament_id,
            rounds=[RoundResponse.of(round_) for round_ in bracket.rounds],
            participants=_participants_of(cls.participant_ids_in(bracket), profiles or {}),
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

    `participants` carries the same identities the bracket's does and for
    the same reason (§26): a placing table of raw identifiers is not a
    result anybody can read, and resolving them client-side is one lookup
    per entrant.
    """

    tournament_id: UUID
    standings: list[StandingResponse]
    participants: list[TournamentParticipantResponse] = Field(
        default_factory=list,
        description="Every player id appearing in the standings, resolved to a public identity.",
    )

    @staticmethod
    def participant_ids_in(standings: list[StandingView]) -> list[UUID]:
        """Every distinct player a placing names — entrants and eliminators.

        `eliminated_by_player_id` is one of the entrants in a
        single-elimination bracket, so the set is almost always just the
        field. It is unioned in rather than assumed away, because assuming
        it makes this mapper depend on a format rule it does not own.
        """
        seen = {standing.player_id for standing in standings}
        seen |= {
            standing.eliminated_by_player_id
            for standing in standings
            if standing.eliminated_by_player_id is not None
        }
        return sorted(seen, key=str)

    @classmethod
    def of(
        cls,
        tournament_id: UUID,
        standings: list[StandingView],
        profiles: Mapping[UUID, PublicUserProfile] | None = None,
    ) -> "StandingsResponse":
        return cls(
            tournament_id=tournament_id,
            standings=[StandingResponse.of(standing) for standing in standings],
            participants=_participants_of(cls.participant_ids_in(standings), profiles or {}),
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
    "TournamentListResponse",
    "TournamentParticipantResponse",
    "TournamentResponse",
    "decode_cursor",
    "decode_list_cursor",
    "encode_cursor",
    "encode_list_cursor",
]
