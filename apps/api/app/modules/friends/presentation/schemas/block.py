"""Wire schemas for blocking — A64-013.5.

## A blocked player is a full public profile, composed

Each item embeds `ProfileResponse`, like every other list on this platform —
A64-013.1 established one public representation of a player and this is the
fifth place it appears.

**And it is the one place a `BLOCKED` relationship composes something the
caller can see.** The blocker is a party to their own block, so a block list
that hid the people on it would be unusable: you cannot lift a block on
somebody you cannot identify. The composition therefore runs with the
relationship the *page* defines rather than the one the graph would resolve
— see the router, which states it.

That is not a privacy hole. What a blocker sees here is what they could see
before blocking; what the *blocked* player sees is unchanged and they are
never told the block exists (BL-1).

## What is deliberately absent

No block id. `DELETE /blocks/{player_id}` is keyed on the *player*, because
that is what a client has in front of it and a block id would be a second
identifier for a relationship the caller can already name.

No `blocker_id` — it is always you. No indication of whether the other party
has blocked *you*: that is the one fact this API must never surface, and the
shape is what guarantees it rather than a filter somebody remembers.
"""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.core.dto import BaseRequestDTO, BaseResponseDTO
from app.modules.friends.domain.block import Block
from app.modules.profiles.presentation.schemas import ProfileResponse


class BlockedPlayerResponse(BaseResponseDTO):
    """One blocked player, with the date the block was placed."""

    player: ProfileResponse = Field(
        description=(
            "The player you blocked — the same public profile shape "
            "`GET /profiles/{username}` returns, so you can recognise who you are "
            "about to unblock."
        ),
    )
    blocked_at: datetime = Field(
        description="When you placed the block, UTC.",
        examples=["2026-08-01T12:00:00Z"],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "player": {
                        "id": "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
                        "username": "player_one",
                        "display_name": "Player One",
                        "avatar_url": None,
                        "thumbnail_url": None,
                        "country": "GB",
                        "language": "en",
                        "bio": None,
                        "joined_at": "2026-07-01T09:30:00Z",
                        "is_online": None,
                        "last_seen": None,
                        "ratings": {
                            "classic": {
                                "rating": 1500,
                                "is_provisional": True,
                                "games_played": 0,
                            },
                            "rapid": {"rating": 1500, "is_provisional": True, "games_played": 0},
                            "blitz": {"rating": 1500, "is_provisional": True, "games_played": 0},
                        },
                        "statistics": {
                            "games_played": 0,
                            "wins": 0,
                            "losses": 0,
                            "draws": 0,
                            "win_rate": 0.0,
                            "current_rating": 1500,
                            "highest_rating": 1500,
                            "current_streak": 0,
                            "best_win_streak": 0,
                        },
                    },
                    "blocked_at": "2026-08-01T12:00:00Z",
                }
            ]
        }
    }

    @classmethod
    def of(cls, block: Block, player: ProfileResponse) -> "BlockedPlayerResponse":
        """Renders one block beside an already-composed profile.

        `player` arrives **rendered**: this schema holds no provider and
        cannot compose anything, which is what keeps batch composition in
        the router.

        Field by field rather than `model_validate(block)`, for the reason
        `users.application.mappers` gives: the aggregate carries
        `blocker_id` and its own id, and an implicit conversion is how one
        of them reaches a response.
        """
        return cls(player=player, blocked_at=block.created_at)


class BlockPlayerRequest(BaseRequestDTO):
    """The `POST /blocks` body.

    One field, and it is a `player_id` rather than a username — the same
    choice `SendFriendRequestRequest` makes, for the same reason: a client
    blocking somebody is looking at a search result, a profile or a match
    card, all of which carry `id`, and DM-06 makes it the only reference
    that crosses a context boundary.
    """

    player_id: UUID = Field(
        description=(
            "The player to block. Blocking ends any friendship between you, voids "
            "any pending friend request in either direction, and removes them from "
            "your search results — all at once. They are **not** notified."
        ),
        examples=["019fb9ea-0a0c-7cec-9c5f-402727c31a96"],
    )

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"examples": [{"player_id": "019fb9ea-0a0c-7cec-9c5f-402727c31a96"}]},
    }
