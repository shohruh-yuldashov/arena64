"""`PublicUserResponse` — the safe by-id view of a player, A64-012.6.

## What this replaces, and why it had to be replaced

`GET /users/{user_id}` returned `UserRead` to **anyone, unauthenticated**.
`UserRead` carries `email`, `is_verified`, `is_active`, `bio`, `country`,
`preferred_language`, `timezone`, `created_at`, `updated_at` and the raw
avatar `object_key`. A64-010's router said so out loud — "returns an email
address to anyone who asks ... safe only because nothing is deployed" — and
every task since has widened the shape rather than closing it: A64-012.1
added `bio` and `country`, A64-012.2 replaced the avatar URL with a storage
key, A64-012.3 added more.

A64-012.6 closes it. The endpoint stays, because a by-id lookup is a real
need — every cross-context reference on this platform is a `player_id`
(DM-06), so a match card, a leaderboard row or a moderation queue holds ids
and not usernames, and there is no other route that turns one into a name.
What changes is what it answers with.

## Why this shape and not `ProfileResponse`

`GET /profiles/{username}` already exists and is richer. This is
deliberately *thinner* than that rather than a second copy of it: four
fields, no bio, no country, no ratings, no statistics, no join date.

Two reasons. A caller resolving an id to a name needs a name, not a
profile — and the fields it does not need are exactly the ones privacy
settings govern, so a thin shape has nothing to gate. And a second full
profile view would be a second place every future privacy rule has to be
applied, which is how one of them eventually gets missed.

`UserSummary` — the published DTO A64-010 designed for precisely this ("a
list, a search result, or a future match card") — is what this renders,
with the avatar reference turned into URLs so no storage key reaches the
wire.

## What is deliberately absent

No `email`, no `is_verified`, no `is_active`, no `locked_until`, no
timestamps, no `object_key`. The account-state fields are the interesting
omissions: publishing them tells an attacker which accounts are
half-registered or currently locked, which is the same argument
`PublicUserProfile` makes at more length.
"""

from uuid import UUID

from pydantic import Field

from app.core.dto import BaseResponseDTO
from app.modules.avatars.public import AvatarLinks
from app.modules.users.public.dtos import UserSummary


class PublicUserResponse(BaseResponseDTO):
    """A player, by id, as anyone may see them."""

    id: UUID = Field(
        description=(
            "The player identifier — stable, public, and the value every other "
            "resource refers to this player by."
        ),
        examples=["019fb9ea-0a0c-7cec-9c5f-402727c31a96"],
    )
    username: str = Field(
        description="The handle, in the casing the player chose.",
        examples=["player_one"],
    )
    display_name: str | None = Field(
        description=(
            "A free-form name the player renders under. `null` when unset — "
            "clients should fall back to `username`."
        ),
        examples=["Player One"],
    )
    avatar_url: str | None = Field(
        description=(
            "URL of the player's avatar at up to 512px, or `null`. Carries a `?v=` "
            "cache-buster that changes on every upload and delete."
        ),
        examples=[None],
    )
    thumbnail_url: str | None = Field(
        description="URL of the 128px rendition, for listings and match cards. `null` when unset.",
        examples=[None],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
                    "username": "player_one",
                    "display_name": "Player One",
                    "avatar_url": None,
                    "thumbnail_url": None,
                }
            ]
        }
    }

    @classmethod
    def of(cls, user: UserSummary, avatar: AvatarLinks | None) -> "PublicUserResponse":
        """Renders the published summary.

        Field by field rather than `model_validate(user)`, and here the
        discipline is the control rather than a style preference: this is
        the endpoint that leaked an email address for four tasks, and an
        implicit conversion is exactly how a field added to `UserSummary`
        would appear on an anonymous response again.

        `avatar` arrives already rendered, so this schema holds no
        `StorageProvider` and cannot compose a URL — the same structure
        `ProfileResponse` has, and the reason no object key reaches the
        wire.
        """
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            avatar_url=avatar.avatar_url if avatar else None,
            thumbnail_url=avatar.thumbnail_url if avatar else None,
        )
