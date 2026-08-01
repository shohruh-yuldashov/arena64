"""Wire schemas for friend requests — A64-013.2.

## The other party is a full public profile, composed

Each list item embeds `ProfileResponse` — the *same* shape
`GET /profiles/{username}` and `GET /users/search` return, rendered by the
same `ProfileResponse.of`. A64-013.1 established that as the platform's one
public representation of a player, and a friend-request list is the third
place it appears.

A thinner shape was the alternative and is worse in a way that only shows up
later: a request list is where somebody decides whether to accept, so it
needs enough to recognise a person and judge them — a rating, a country, a
join date — which is precisely `ProfileResponse` minus nothing. Defining a
`FriendRequestPlayer` with four of its fields would be a second public view
of a player, and every privacy rule would then have two places to be
applied.

**The privacy behaviour comes with it.** These profiles are composed by
`PublicProfileComposer`, so a hidden country is `null` here exactly as it is
on a profile page, and a `null` never says which of its reasons applies.

## What is deliberately absent

No `requester_id` and no `addressee_id`. A64-013.2: "never expose
unnecessary user identifiers." The list already says which direction it is
— `incoming` means the other party sent it — and `player.id` is the public
`player_id` a client needs to act on. Publishing both parties' ids on every
row would add nothing a caller cannot already derive and would make each row
a social-graph edge in a response that is often cached by an HTTP client.

No `version`. It is an optimistic-concurrency token, not a fact about the
relationship: exposing it would invite a client to send it back, which would
turn an internal storage concern into an API contract.

No `expires_at`. Nothing populates it (A64-013.2 excludes expiry), and a
field that is always `null` on every row of every response teaches clients
nothing except to ignore it. It is in the database and the aggregate, ready;
it reaches the wire in the release that gives it a value.
"""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.core.dto import BaseRequestDTO, BaseResponseDTO
from app.modules.friends.domain.friend_request import FriendRequest, FriendRequestStatus
from app.modules.profiles.presentation.schemas import ProfileResponse


class FriendRequestResponse(BaseResponseDTO):
    """One friend request, with the other party's public profile."""

    id: UUID = Field(
        description=(
            "The request identifier. Pass it to the accept, decline and cancel "
            "endpoints — it is the only handle a client needs, and it is scoped to "
            "the two players involved."
        ),
        examples=["019fb9ea-1b2c-7def-8a45-90ab12cd34ef"],
    )
    status: FriendRequestStatus = Field(
        description=(
            "Where the request is in its lifecycle. Both list endpoints return only "
            "`pending` today; the other values appear in the response to the action "
            "that produced them — `accepted`, `declined` or `cancelled`."
        ),
        examples=["pending"],
    )
    player: ProfileResponse = Field(
        description=(
            "The **other** party — the sender on an incoming request, the recipient "
            "on an outgoing one. Identical in shape and in privacy behaviour to "
            "`GET /profiles/{username}`: a hidden country, record or presence is "
            "`null` here for the same reasons and says as little about why."
        ),
    )
    created_at: datetime = Field(
        description="When the request was sent, UTC.",
        examples=["2026-08-01T12:00:00Z"],
    )
    responded_at: datetime | None = Field(
        default=None,
        description=(
            "When the request was resolved, UTC. `null` while it is `pending` — the "
            "two always agree, because a database CHECK enforces the pairing."
        ),
        examples=[None],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": "019fb9ea-1b2c-7def-8a45-90ab12cd34ef",
                    "status": "pending",
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
                    "created_at": "2026-08-01T12:00:00Z",
                    "responded_at": None,
                }
            ]
        }
    }

    @classmethod
    def of(cls, request: FriendRequest, player: ProfileResponse) -> "FriendRequestResponse":
        """Renders one request beside an already-composed profile.

        `player` arrives **rendered**, not as a player id and not as a
        composer: this schema holds no provider, cannot compose anything and
        cannot reach a privacy flag. That is the same structure
        `ProfileResponse` has with `AvatarLinks`, and it is what keeps the
        batch composition in the router — a schema that could compose would
        be a schema somebody eventually calls in a loop.

        Field by field rather than `model_validate(request)`, for the reason
        `users.application.mappers` gives: the aggregate carries `version`
        and both party ids, and an implicit conversion is how one of them
        reaches an anonymous-adjacent response.
        """
        return cls(
            id=request.id,
            status=request.status,
            player=player,
            created_at=request.created_at,
            responded_at=request.responded_at,
        )


class SendFriendRequestRequest(BaseRequestDTO):
    """The `POST /friends/requests` body.

    One field, and it is a `player_id` rather than a username. Both were
    plausible; the id wins because it is what a client already holds
    everywhere it would send this from — a search result, a profile page, a
    match card all carry `id`, and DM-06 makes it the only reference that
    crosses a context boundary. Resolving a username here would add a lookup
    whose *timing* answers "does this handle exist" on an authenticated
    write endpoint.
    """

    player_id: UUID = Field(
        description=(
            "The player to send a request to — the `id` from a search result or a "
            "profile. Sending to yourself is a `422`; sending to somebody who "
            "already has a pending request from you, or who has one pending to you, "
            "is a `409` whose `code` says which."
        ),
        examples=["019fb9ea-0a0c-7cec-9c5f-402727c31a96"],
    )

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {"examples": [{"player_id": "019fb9ea-0a0c-7cec-9c5f-402727c31a96"}]},
    }
