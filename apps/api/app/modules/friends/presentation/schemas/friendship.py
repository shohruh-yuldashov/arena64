"""Wire schemas for the friend list — A64-013.3.

## A friend is a full public profile, composed

Each item embeds `ProfileResponse` — the same shape
`GET /profiles/{username}`, `GET /users/search` and the friend-request lists
return, rendered by the same `ProfileResponse.of`. A64-013.1 established
that as the platform's one public representation of a player; the friend
list is the fourth place it appears.

**And it is the first place `VisibilityLevel.FRIENDS` matters.** Every
player in this list is, by definition, a friend of the caller — so a field
they restricted to friends is visible here and hidden on the same profile
read by a stranger. That falls out of the composer resolving the
relationship per player; nothing in this file knows about it.

## What is deliberately absent

No `friendship_id`. A64-013.4 may need one for a management surface, but
nothing here does: `DELETE /friends/{player_id}` is keyed on the *player*,
because that is what a client has in front of it and because a friendship id
would be a second identifier for a relationship a caller can already name.

No `player_low_id`/`player_high_id`. Canonical ordering is a storage
concern (DB-12) and means nothing to a client — publishing it would leak an
implementation detail and invite somebody to depend on the ordering.

No `ended_at`. This list contains live friendships only; the field would be
`null` on every row of every response.
"""

from datetime import datetime

from pydantic import Field

from app.core.dto import BaseResponseDTO
from app.modules.friends.domain.friendship import Friendship
from app.modules.profiles.presentation.schemas import ProfileResponse


class FriendResponse(BaseResponseDTO):
    """One friend, with the date the friendship began."""

    player: ProfileResponse = Field(
        description=(
            "Your friend's public profile — identical in shape to "
            "`GET /profiles/{username}`. Fields they have restricted to friends **are "
            "visible here**, because you are one; the same profile read by a stranger "
            "hides them."
        ),
    )
    friends_since: datetime = Field(
        description=(
            "When the friendship began, UTC — the instant the friend request was "
            "accepted, and the same instant that request records as its response."
        ),
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
                        "is_online": True,
                        "last_seen": "2026-08-01T15:44:00Z",
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
                    "friends_since": "2026-08-01T12:00:00Z",
                }
            ]
        }
    }

    @classmethod
    def of(cls, friendship: Friendship, player: ProfileResponse) -> "FriendResponse":
        """Renders one friendship beside an already-composed profile.

        `player` arrives **rendered**: this schema holds no provider, cannot
        compose anything and cannot reach a privacy flag — the structure
        `FriendRequestResponse` has, and what keeps batch composition in the
        router rather than in a schema somebody eventually calls in a loop.

        Field by field rather than `model_validate`, for the reason
        `users.application.mappers` gives: the aggregate carries both party
        ids and the canonical ordering, and an implicit conversion is how
        one of them reaches a response.
        """
        return cls(player=player, friends_since=friendship.created_at)


class FriendCountResponse(BaseResponseDTO):
    """How many friends you have.

    Its own shape rather than a bare integer, because a JSON response body
    that is a number has nowhere to grow: A64-013.4's management surface
    will plausibly want a pending-request count beside this one, and adding
    a field to an object is additive while changing `42` into `{"total": 42}`
    is not.
    """

    total: int = Field(
        description=(
            "Your current number of friends. Counts live friendships only — a "
            "friendship that ended is not included, in either direction."
        ),
        examples=[7],
        ge=0,
    )

    model_config = {"json_schema_extra": {"examples": [{"total": 7}]}}
