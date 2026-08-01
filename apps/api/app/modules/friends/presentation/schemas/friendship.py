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
from app.modules.friends.domain.friendship import Friendship, FriendshipMetadata
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


class FriendshipDetailsResponse(BaseResponseDTO):
    """One friendship, inspected — A64-013.4's `GET /friends/{player_id}`.

    Deliberately the **same two fields** as `FriendResponse` above, and not
    merged with it. They answer different questions and will diverge: a list
    item is what a row renders, while this is what a relationship page
    shows, and the first field to arrive on one and not the other is
    whatever A64-013.4's successors add — a mutual-friend count, shared
    match history, a "friends for 3 months" badge.

    Merging them now would save five lines and would make the first
    divergence a breaking change to the list.

    ## What is deliberately absent

    No friendship id, no `player_low_id`, no `player_high_id`, no
    `ended_at`. A64-013.4: "do NOT expose internal database fields." The
    canonical ordering is a storage concern (DB-12) that means nothing to a
    client; a friendship id is a second name for a relationship already
    addressed by the other player's id; and this endpoint only ever
    describes a live friendship, so an end date would be `null` on every
    response.

    **No mutual friend count**, which the read model behind this *does*
    carry. A64-013.4 scopes mutual counts to "repository/service only", so
    the number is computed, tested and unpublished — the field appears here
    the day a UX surface asks for it, additively.
    """

    player: ProfileResponse = Field(
        description=(
            "Your friend's public profile — identical in shape to "
            "`GET /profiles/{username}`. Fields they have restricted to friends **are "
            "visible here**, because you are one."
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
    def of(
        cls, metadata: FriendshipMetadata, player: ProfileResponse
    ) -> "FriendshipDetailsResponse":
        """Renders the read model beside an already-composed profile.

        `player` arrives **rendered**: this schema holds no provider and
        cannot compose anything — the structure every response schema on
        this platform has, and what keeps composition in the router.

        `metadata.mutual_friend_count` is **not mapped**, deliberately. It
        is computed and available; publishing it is a later task's decision,
        and a field that is not in this constructor cannot leak by
        accident.
        """
        return cls(player=player, friends_since=metadata.friends_since)
