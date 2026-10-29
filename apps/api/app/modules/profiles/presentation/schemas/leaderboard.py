"""Wire shapes for the leaderboard with names on it — A64-033.

A row is what a stranger may already see about a player — their handle,
display name and *visible* country, from `users`' public view — beside what
`rating` publishes about their standing. Nothing else: no email, no avatar
key, no deviation's internals, no reason anybody is absent.
"""

import base64
import binascii
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.error_codes import ErrorCode
from app.core.exceptions import ValidationError
from app.modules.profiles.application.services.leaderboard_service import (
    LadderPosition,
    LeaderboardView,
    OwnStanding,
    RankedPlayer,
    StandingStatus,
)
from app.modules.rating.public import LeaderboardCursor, RatingKey

_SEPARATOR = "|"


class InvalidCursor(ValidationError):
    """The `after` parameter was not a cursor this API issued."""

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_CURSOR


def encode_position(position: LadderPosition) -> str:
    """Opaque, like every cursor on this API: the three ordering values and
    how many players the walk has ranked, so the next page numbers on."""
    after = position.after
    raw = _SEPARATOR.join(
        (str(after.rating), str(after.deviation), str(after.player_id), str(position.ranked))
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_position(value: str) -> LadderPosition:
    """Every way a cursor can be malformed is one error — the answer is
    always "start from the top", and telling them apart would narrate the
    encoding to whoever is probing it."""
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        rating, deviation, player_id, ranked = raw.split(_SEPARATOR)
        count = int(ranked)
        if count < 0:
            raise ValueError("negative rank")
        return LadderPosition(
            after=LeaderboardCursor(
                rating=float(rating), deviation=float(deviation), player_id=UUID(player_id)
            ),
            ranked=count,
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as malformed:
        raise InvalidCursor("the pagination cursor is not valid") from malformed


class LeaderboardPlayerResponse(BaseModel):
    rank: int = Field(description="Position among the players this ladder shows. Unique.")
    player_id: UUID
    username: str
    display_name: str | None
    country: str | None = Field(
        description="ISO 3166-1 alpha-2, or `null` when unset **or hidden by its owner**."
    )
    rating: float
    games_played: int
    is_provisional: bool

    @classmethod
    def of(cls, ranked: RankedPlayer) -> "LeaderboardPlayerResponse":
        return cls(
            rank=ranked.rank,
            player_id=ranked.entry.player_id,
            username=ranked.profile.username,
            display_name=ranked.profile.display_name,
            country=ranked.profile.country,
            rating=ranked.entry.rating,
            games_played=ranked.entry.games_played,
            is_provisional=ranked.entry.is_provisional,
        )


class LeaderboardPlayersResponse(BaseModel):
    variant: str
    speed_class: str
    country: str | None = Field(description="The filter applied; `null` for the global ladder.")
    players: list[LeaderboardPlayerResponse]
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque. `null` once the ladder is exhausted. A page shorter than "
            "`limit` that still carries a cursor stopped at the scan budget: "
            "more players may follow."
        ),
    )

    @classmethod
    def of(
        cls, key: RatingKey, country: str | None, view: LeaderboardView
    ) -> "LeaderboardPlayersResponse":
        return cls(
            variant=key.variant.value,
            speed_class=key.speed_class.value,
            country=country,
            players=[LeaderboardPlayerResponse.of(player) for player in view.players],
            next_cursor=encode_position(view.next) if view.next is not None else None,
        )


class LeaderboardStandingResponse(BaseModel):
    """The caller's own place. Only ever about the caller."""

    variant: str
    speed_class: str
    country: str | None
    status: StandingStatus = Field(
        description=(
            "`ranked`: at `rank`. `beyond`: on this ladder, further down than an "
            "exact rank is counted. `not_listed`: rated, but not shown on this "
            "ladder. `unrated`: no rating in this speed class."
        )
    )
    rank: int | None
    rating: float | None
    games_played: int | None
    is_provisional: bool | None

    @classmethod
    def of(
        cls, key: RatingKey, country: str | None, standing: OwnStanding
    ) -> "LeaderboardStandingResponse":
        entry = standing.entry
        return cls(
            variant=key.variant.value,
            speed_class=key.speed_class.value,
            country=country,
            status=standing.status,
            rank=standing.rank,
            rating=entry.rating if entry is not None else None,
            games_played=entry.games_played if entry is not None else None,
            is_provisional=entry.is_provisional if entry is not None else None,
        )


__all__ = [
    "InvalidCursor",
    "LeaderboardPlayersResponse",
    "LeaderboardStandingResponse",
    "decode_position",
    "encode_position",
]
