"""Wire shapes for match history and replay — SPEC-REPLAY §4.

Pydantic models built from `game.public` values, never from ORM rows. No
internal identifier, no column name, no exception type reaches a client.

## The cursor is opaque

A client receives `next_cursor` as one string and sends it back unread. Its
contents are `(created_at, match_id)` — an implementation detail of the
ordering, and publishing them as fields would make the ordering a contract
that cannot change without breaking clients.

Encoded, not encrypted: it carries nothing a caller could not see in the
page it came from. What the opacity buys is freedom to change the ordering
keys, and a `400 invalid_cursor` for anything hand-made.
"""

import base64
import binascii
from collections.abc import Mapping
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.error_codes import ErrorCode
from app.core.exceptions import ValidationError
from app.modules.game.public import (
    HistoryCursor,
    MatchHistoryEntry,
    MatchHistoryPage,
    MatchReplay,
    ReplayPly,
    ReplaySeat,
)
from app.modules.users.public import PublicUserProfile

_CURSOR_SEPARATOR = "|"


class InvalidCursor(ValidationError):
    """The `after` parameter was not a cursor this API issued."""

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_CURSOR


def encode_cursor(cursor: HistoryCursor) -> str:
    raw = f"{cursor.created_at.isoformat()}{_CURSOR_SEPARATOR}{cursor.match_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(value: str) -> HistoryCursor:
    """A cursor string as the port's value. Raises `InvalidCursor`.

    Every failure mode collapses to one error: bad base64, a missing
    separator, an unparseable instant or id. A client cannot act differently
    on any of them — the answer is always "ask for the first page" — and
    distinguishing them would describe the encoding to whoever is probing it.
    """
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        instant, _, match_id = raw.partition(_CURSOR_SEPARATOR)
        return HistoryCursor(created_at=datetime.fromisoformat(instant), match_id=UUID(match_id))
    except (binascii.Error, UnicodeDecodeError, ValueError) as malformed:
        raise InvalidCursor("the pagination cursor is not valid") from malformed


class MatchHistoryEntryResponse(BaseModel):
    """One finished match, as a list renders it.

    `opponent_id` is present only when the viewer played — it is the field
    that makes a personal history readable, and it is meaningless when a
    stranger reads somebody else's record.
    """

    match_id: UUID
    variant: str
    speed_class: str | None = Field(
        default=None,
        description="The rating key's speed class, when the match recorded one.",
    )
    rated: bool
    engine_version: int = Field(
        description="Replay is refused for versions this build cannot reproduce."
    )

    light_player_id: UUID
    dark_player_id: UUID
    opponent_id: UUID | None = Field(
        default=None, description="The other player, when the viewer is a participant."
    )

    outcome: str | None
    termination_reason: str | None
    winner: str | None
    ply_number: int

    started_at: datetime
    ended_at: datetime | None

    @classmethod
    def of(cls, entry: MatchHistoryEntry, *, viewer_id: UUID) -> "MatchHistoryEntryResponse":
        opponent = _opponent_of(entry, viewer_id)
        return cls(
            match_id=entry.match_id,
            variant=entry.variant.value,
            speed_class=None,
            rated=entry.rated,
            engine_version=entry.engine_version,
            light_player_id=entry.light_player_id,
            dark_player_id=entry.dark_player_id,
            opponent_id=opponent,
            outcome=entry.outcome.value if entry.outcome else None,
            termination_reason=(
                entry.termination_reason.value if entry.termination_reason else None
            ),
            winner=entry.winner.value if entry.winner else None,
            ply_number=entry.ply_number,
            started_at=entry.created_at,
            ended_at=entry.ended_at,
        )


class MatchHistoryResponse(BaseModel):
    entries: list[MatchHistoryEntryResponse]
    next_cursor: str | None = Field(
        default=None, description="Opaque. Send it back as `after`; `null` on the last page."
    )

    @classmethod
    def of(cls, page: MatchHistoryPage, *, viewer_id: UUID) -> "MatchHistoryResponse":
        return cls(
            entries=[
                MatchHistoryEntryResponse.of(entry, viewer_id=viewer_id) for entry in page.entries
            ],
            next_cursor=encode_cursor(page.next_cursor) if page.next_cursor else None,
        )


class PlacedPieceResponse(BaseModel):
    square: str
    side: str
    rank: str


class ReplayPlyResponse(BaseModel):
    """One ply, and the board it produced."""

    ply_number: int
    side: str
    path: list[str]
    captured: list[str]
    promoted_to: str | None
    fingerprint: str
    pieces: list[PlacedPieceResponse]
    think_time_ms: int | None
    remaining_clock_ms: int | None

    @classmethod
    def of(cls, ply: ReplayPly) -> "ReplayPlyResponse":
        return cls(
            ply_number=ply.ply_number,
            side=ply.side.value,
            path=list(ply.path),
            captured=list(ply.captured),
            promoted_to=ply.promoted_to,
            fingerprint=ply.fingerprint,
            pieces=[
                PlacedPieceResponse(square=p.square, side=p.side, rank=p.rank) for p in ply.pieces
            ],
            think_time_ms=ply.think_time_ms,
            remaining_clock_ms=ply.remaining_clock_ms,
        )


class ReplayTimeControlResponse(BaseModel):
    """How much time each side had. `null` for an untimed match."""

    initial_ms: int
    increment_ms: int


class ReplaySeatResponse(BaseModel):
    """One seat, with as much of its player as the viewer may see —
    A64-020.5E §13.

    The identity is composed from `users`' **public** profile read, gated
    by the same privacy policy every other surface uses. `username` and
    `display_name` are `None` for a deactivated account, which is the
    answer this platform gives everywhere rather than a special case here.

    The rating is `game`'s own: the snapshot taken when the match was
    created (PR-3). A replay shows what the game was played at, not what
    the player rates now.
    """

    player_id: UUID
    username: str | None = None
    display_name: str | None = None
    avatar_thumbnail_url: str | None = None
    rating_value: float | None = None
    rating_deviation: float | None = None
    is_provisional: bool | None = None

    @classmethod
    def of(cls, seat: ReplaySeat, profile: PublicUserProfile | None) -> "ReplaySeatResponse":
        return cls(
            player_id=seat.player_id,
            username=profile.username if profile else None,
            display_name=profile.display_name if profile else None,
            avatar_thumbnail_url=getattr(profile, "avatar_thumbnail_url", None),
            rating_value=seat.rating_value,
            rating_deviation=seat.rating_deviation,
            is_provisional=seat.is_provisional,
        )


class MatchReplayResponse(BaseModel):
    """A whole finished game, reconstructed, with the metadata that
    describes it — A64-020.5E §5, §14.

    The metadata is here because there is nowhere else it could come from:
    no endpoint serves one match's stored facts by id, and a client would
    otherwise have had to page a player's whole history to learn whether
    the game it is replaying was rated.
    """

    match_id: UUID
    variant: str
    engine_version: int
    status: str
    rated: bool
    speed_class: str | None
    time_control: ReplayTimeControlResponse | None
    light: ReplaySeatResponse
    dark: ReplaySeatResponse
    created_at: datetime
    ended_at: datetime | None
    opening: list[PlacedPieceResponse]
    plies: list[ReplayPlyResponse]
    outcome: str | None
    termination_reason: str | None
    winner: str | None

    @classmethod
    def of(
        cls,
        replay: MatchReplay,
        profiles: Mapping[UUID, PublicUserProfile] | None = None,
    ) -> "MatchReplayResponse":
        seen = profiles or {}
        return cls(
            match_id=replay.match_id,
            variant=replay.variant.value,
            engine_version=replay.engine_version,
            status=replay.status.value,
            rated=replay.rated,
            speed_class=replay.speed_class,
            time_control=(
                ReplayTimeControlResponse(
                    initial_ms=replay.time_control.initial_ms,
                    increment_ms=replay.time_control.increment_ms,
                )
                if replay.time_control is not None
                else None
            ),
            light=ReplaySeatResponse.of(replay.light, seen.get(replay.light.player_id)),
            dark=ReplaySeatResponse.of(replay.dark, seen.get(replay.dark.player_id)),
            created_at=replay.created_at,
            ended_at=replay.ended_at,
            opening=[
                PlacedPieceResponse(square=p.square, side=p.side, rank=p.rank)
                for p in replay.opening
            ],
            plies=[ReplayPlyResponse.of(ply) for ply in replay.plies],
            outcome=replay.outcome.value if replay.outcome else None,
            termination_reason=(
                replay.termination_reason.value if replay.termination_reason else None
            ),
            winner=replay.winner.value if replay.winner else None,
        )


def _opponent_of(entry: MatchHistoryEntry, viewer_id: UUID) -> UUID | None:
    if viewer_id == entry.light_player_id:
        return entry.dark_player_id
    if viewer_id == entry.dark_player_id:
        return entry.light_player_id
    return None


__all__ = [
    "InvalidCursor",
    "MatchHistoryEntryResponse",
    "MatchHistoryResponse",
    "MatchReplayResponse",
    "ReplaySeatResponse",
    "ReplayTimeControlResponse",
    "decode_cursor",
    "encode_cursor",
]
