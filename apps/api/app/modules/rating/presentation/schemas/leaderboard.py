"""Wire shapes for the ladder — A64-020.0A.

## The cursor is opaque

A client receives `next_cursor` as one string and sends it back unread. Its
contents are the three ordering values — an implementation detail of the
ordering — and publishing them as fields would make the ordering a contract
that cannot change without breaking clients. The same choice match history
and tournament history both make.

Encoded, not encrypted: it carries nothing a caller could not read in the
page it came from.

## No profile fields

An entry is a `player_id` and a standing. Handle, avatar and country are
`profiles`', and a leaderboard row that carried them would make every
ranking read a join and make `rating` depend on a module it has no business
knowing about. A client renders names by asking `profiles` for the ids it
just received — one batched call per page, not one per row.
"""

import base64
import binascii
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.error_codes import ErrorCode
from app.core.exceptions import ValidationError
from app.modules.rating.public import (
    LeaderboardCursor,
    LeaderboardEntry,
    LeaderboardPage,
    RatingKey,
)
from app.modules.rating.public.leaderboard import LeaderboardNeighbourhood

_CURSOR_SEPARATOR = "|"


class InvalidCursor(ValidationError):
    """The `after` parameter was not a cursor this API issued."""

    default_code: ClassVar[ErrorCode] = ErrorCode.INVALID_CURSOR


def encode_cursor(cursor: LeaderboardCursor) -> str:
    raw = (
        f"{cursor.rating}{_CURSOR_SEPARATOR}{cursor.deviation}{_CURSOR_SEPARATOR}{cursor.player_id}"
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(value: str) -> LeaderboardCursor:
    """A cursor string as the port's value. Raises `InvalidCursor`.

    Every failure collapses to one error: bad base64, a missing separator,
    an unparseable float or id. A client cannot act differently on any of
    them — the answer is always "ask for the first page" — and
    distinguishing them would describe the encoding to whoever is probing it.
    """
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        rating, deviation, player_id = raw.split(_CURSOR_SEPARATOR)
        return LeaderboardCursor(
            rating=float(rating), deviation=float(deviation), player_id=UUID(player_id)
        )
    except (binascii.Error, UnicodeDecodeError, ValueError) as malformed:
        raise InvalidCursor("the pagination cursor is not valid") from malformed


class LeaderboardEntryResponse(BaseModel):
    """One row of the ladder."""

    player_id: UUID
    rating: float
    deviation: float
    games_played: int
    is_provisional: bool = Field(
        description="Provisional players are ranked and shown, never hidden."
    )

    @classmethod
    def of(cls, entry: LeaderboardEntry) -> "LeaderboardEntryResponse":
        return cls(
            player_id=entry.player_id,
            rating=entry.rating,
            deviation=entry.deviation,
            games_played=entry.games_played,
            is_provisional=entry.is_provisional,
        )


class LeaderboardResponse(BaseModel):
    """One page of one key's ladder, best first."""

    variant: str
    speed_class: str
    entries: list[LeaderboardEntryResponse]
    next_cursor: str | None = Field(
        default=None, description="Opaque; send it back unread for the next page."
    )

    @classmethod
    def of(cls, key: RatingKey, page: LeaderboardPage) -> "LeaderboardResponse":
        return cls(
            variant=key.variant.value,
            speed_class=key.speed_class.value,
            entries=[LeaderboardEntryResponse.of(entry) for entry in page.entries],
            next_cursor=encode_cursor(page.next_cursor) if page.next_cursor else None,
        )


class LeaderboardNeighbourhoodResponse(BaseModel):
    """Where one player stands, and who is next to them.

    `rank` counts from 1 and is **unique**: the ladder's ordering is total,
    so no two players share a position. That is deliberately unlike a
    tournament's placement, where a shared tier is the product rule.
    """

    variant: str
    speed_class: str
    rank: int
    entry: LeaderboardEntryResponse
    above: list[LeaderboardEntryResponse] = Field(
        description="Rows immediately better, nearest last."
    )
    below: list[LeaderboardEntryResponse] = Field(
        description="Rows immediately worse, nearest first."
    )

    @classmethod
    def of(
        cls, key: RatingKey, neighbourhood: LeaderboardNeighbourhood
    ) -> "LeaderboardNeighbourhoodResponse":
        return cls(
            variant=key.variant.value,
            speed_class=key.speed_class.value,
            rank=neighbourhood.rank,
            entry=LeaderboardEntryResponse.of(neighbourhood.entry),
            above=[LeaderboardEntryResponse.of(entry) for entry in neighbourhood.above],
            below=[LeaderboardEntryResponse.of(entry) for entry in neighbourhood.below],
        )


__all__ = [
    "InvalidCursor",
    "LeaderboardEntryResponse",
    "LeaderboardNeighbourhoodResponse",
    "LeaderboardResponse",
    "decode_cursor",
    "encode_cursor",
]
