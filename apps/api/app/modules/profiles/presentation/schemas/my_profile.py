"""Wire schemas for the self-service profile endpoints — A64-012.3.

Two types: what a client may send to `PATCH /profile`, and what both
`PATCH /profile` and `GET /profile/me` return.

## The request type is where mass assignment is stopped, twice

`ProfileUpdateRequest` declares exactly three fields and inherits
`extra="forbid"` from `BaseRequestDTO`. A client sending `username`,
`is_verified`, `avatar_object_key` or `id` gets a `422` naming the field —
it is never silently applied and never silently dropped.

A64-012.3 says "ignore unknown fields". This **rejects** them instead, and
the deviation is deliberate: rejecting satisfies the actual requirement —
"prevent mass assignment" — more strongly than ignoring, because ignoring
leaves a client believing a write happened. It also matches every other
request schema on the platform (A64-011.9's audit added a test asserting
`extra="forbid"` across all of them), and a single endpoint that quietly
swallowed typos would be the one place a misspelled `display_nmae` looked
like a success.

The second layer is the published `ProfileEdits` type, which has the same
three attributes and no `**extra`. Even a caller bypassing this schema
cannot set a fourth field.

## What A64-012.5 moved out

`preferred_language` and `timezone` were here, on both types. They are
preferences, and leaving them writable from a profile edit as well as from
`PATCH /profile/preferences` would have been the duplicated writable field
that task set out to remove — so they moved, and the *read* moved with the
write. A response whose justification is "the fields this endpoint can
change" must not keep reporting two it cannot.

`GET /auth/me` still reports both; that is the account view, and a
different question.

## Why `UNSET` is not a wire concept

Pydantic cannot express "absent" as a value, so the schema uses `None`
defaults and the *router* reads `model_fields_set` to recover which keys
the client actually sent. That is the same mechanism `PATCH /users/{id}`
used from A64-010; see the router for the mapping, and
`users.public.edits` for why the three states have to stay distinct.
"""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.core.dto import BaseRequestDTO, BaseResponseDTO
from app.modules.avatars.public import AvatarLinks
from app.modules.profiles.presentation.schemas.profile import StatisticsResponse
from app.modules.statistics.public import PlayerStatistics
from app.modules.users.domain.validators import (
    BIO_MAX_LENGTH,
    DISPLAY_NAME_MAX_LENGTH,
    DISPLAY_NAME_MIN_LENGTH,
)
from app.modules.users.public import OwnUserProfile, Presence


class ProfileUpdateRequest(BaseRequestDTO):
    """The `PATCH /profile` body. Every field optional; send only what
    changes.

    Bounds are declared here *as well as* in the domain validators, and the
    duplication is deliberate rather than an oversight: Pydantic's bound
    rejects a multi-megabyte biography while parsing, before the value
    reaches a validator that would have to materialise it first. The domain
    validator remains the authority — it is what a non-HTTP caller hits —
    and both read the same constants, so they cannot disagree about the
    number.
    """

    display_name: str | None = Field(
        default=None,
        min_length=DISPLAY_NAME_MIN_LENGTH,
        max_length=DISPLAY_NAME_MAX_LENGTH,
        description=(
            f"The name shown beside your avatar. {DISPLAY_NAME_MIN_LENGTH}-"
            f"{DISPLAY_NAME_MAX_LENGTH} characters, any script — surrounding "
            "whitespace is trimmed. Send `null` to remove it and fall back to "
            "your username."
        ),
        examples=["Жанибек Алиев"],
    )
    bio: str | None = Field(
        default=None,
        max_length=BIO_MAX_LENGTH,
        description=(
            f"A short self-description — **plain text**, at most {BIO_MAX_LENGTH} "
            "characters. Markdown is not supported and will not be rendered. "
            "Send `null` to remove it."
        ),
        examples=["Blitz player. Occasionally studies the endgame."],
    )
    country: str | None = Field(
        default=None,
        description=(
            "ISO 3166-1 alpha-2 country code — `UZ`, `GB`, `RU`. Case-insensitive "
            "on input, stored upper-cased. Unassigned codes such as `XX` are "
            "rejected. Send `null` to remove it."
        ),
        examples=["UZ"],
    )
    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [
                {
                    "display_name": "Жанибек Алиев",
                    "bio": "Blitz player. Occasionally studies the endgame.",
                    "country": "UZ",
                },
                {"bio": None},
            ]
        },
    }

    # --- removed in A64-012.5: `_reject_explicit_null_on_non_clearable` ---
    #
    # It existed for `preferred_language` and `timezone`, the only two
    # fields here that had no cleared state. Both moved to
    # `PATCH /profile/preferences`, and every field that remains is
    # genuinely nullable — `null` clears a display name, a biography or a
    # country — so there is nothing left for it to reject. The equivalent
    # rule now lives in `schemas/preferences.py`, where the non-clearable
    # fields went.


class MyProfileResponse(BaseResponseDTO):
    """The account holder's own profile, as returned by both endpoints.

    One shape for `GET /profile/me` and `PATCH /profile`, because both
    answer the same question — *what is my profile now* — and a client's
    handling is identical. Returning the full profile from the PATCH also
    means the response carries **normalised** values: a display name sent
    padded comes back trimmed, a country sent `uz` comes back `UZ`. A
    client that echoed its own request would render something the platform
    did not store.

    Carries no `email`, no `is_verified` and no `is_active`. Those are
    `auth`'s and are served by `GET /auth/me`; a second copy here would be
    a second thing to keep in step.
    """

    id: UUID = Field(
        description="Your player identifier — stable and public.",
        examples=["019fb9ea-0a0c-7cec-9c5f-402727c31a96"],
    )
    username: str = Field(
        description=(
            "Your handle. **Read-only** — username changes are not part of this "
            "API. Renaming has to record the previous handle and hold it in a reuse "
            "cooldown, so it will arrive as its own endpoint."
        ),
        examples=["player_one"],
    )
    display_name: str | None = Field(
        description="The name shown beside your avatar, trimmed. `null` when unset.",
        examples=["Жанибек Алиев"],
    )
    bio: str | None = Field(
        description="Your self-description, plain text. `null` when unset.",
        examples=["Blitz player. Occasionally studies the endgame."],
    )
    country: str | None = Field(
        description="ISO 3166-1 alpha-2, upper-cased. `null` when unset.",
        examples=["UZ"],
    )
    avatar_url: str | None = Field(
        description="Your avatar at up to 512px, or `null`. Managed at `/profile/avatar`.",
        examples=[None],
    )
    thumbnail_url: str | None = Field(
        description="The 128px rendition, or `null`.",
        examples=[None],
    )
    joined_at: datetime = Field(
        description="When your account was created, UTC.",
        examples=["2026-08-01T12:00:00Z"],
    )
    is_online: bool | None = Field(
        default=None,
        description=(
            "Whether **you** are shown as connected right now.\n\n"
            "**Never redacted.** `show_online_status` governs what strangers "
            "see on `GET /profiles/{username}`, not what you see of "
            "yourself — a control that hid your own presence from you would "
            "be one you could never verify you had set.\n\n"
            "`null` means the platform has no current observation of you, "
            "not that it is hidden: presence decays on a timer, and nothing "
            "records it yet (see `last_seen`)."
        ),
        examples=[None],
    )
    last_seen: datetime | None = Field(
        default=None,
        description=(
            "When **you** were last observed online, UTC. **Never "
            "redacted** — `show_last_seen` governs strangers, not you.\n\n"
            "`null` today for every account: presence is written by the "
            "realtime gateway, which does not exist yet. The field is in the "
            "contract so a client renders 'unknown' rather than gaining an "
            "unexpected key later."
        ),
        examples=[None],
    )
    statistics: StatisticsResponse = Field(
        description=(
            "Your competitive record. **Always present, never `null`** — "
            "`show_statistics` governs what *strangers* see on "
            "`GET /profiles/{username}`, not what you see of yourself. A "
            "privacy control that hid a setting from the person who set it "
            "would be one nobody could verify they had applied."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": "019fb9ea-0a0c-7cec-9c5f-402727c31a96",
                    "username": "player_one",
                    "display_name": "Жанибек Алиев",
                    "bio": "Blitz player. Occasionally studies the endgame.",
                    "country": "UZ",
                    "avatar_url": None,
                    "thumbnail_url": None,
                    "joined_at": "2026-08-01T12:00:00Z",
                    "is_online": None,
                    "last_seen": None,
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
                }
            ]
        }
    }

    @classmethod
    def of(
        cls,
        profile: OwnUserProfile,
        avatar: AvatarLinks | None,
        statistics: PlayerStatistics,
        presence: Presence | None,
    ) -> "MyProfileResponse":
        """Renders the published owner view.

        Field by field rather than `model_validate(profile)`, for the
        reason `users.application.mappers` gives — and `avatar` arrives
        already rendered, so this schema holds no `StorageProvider` and
        knows no object-key layout, exactly as `ProfileResponse` does.

        `statistics` arrives the same way: already read, through the same
        `StatisticsProvider` the public path uses, and **unredacted**. This
        schema holds no privacy flag it could apply and no provider it
        could call — the owner-always-sees-their-own rule is a property of
        who calls this, not a branch inside it.

        Required rather than optional, unlike `ProfileResponse.statistics`.
        The public shape has to express "hidden"; this one never does.

        `presence` arrives read through the same `PresenceProvider` the
        public path uses, and unredacted for the same reason (A64-012.7:
        "authenticated users may always view their own presence
        information"). `None` here means *unobserved*, never *hidden* — the
        only two flags that could hide it are not consulted on this path.

        **Nothing from `Presence` beyond the two fields.** `session_id` and
        `device_type` are on the record and have no field on this schema to
        land in, which is what makes "never expose internal session
        identifiers" structural rather than remembered.
        """
        return cls(
            id=profile.id,
            username=profile.username,
            display_name=profile.display_name,
            bio=profile.bio,
            country=profile.country,
            avatar_url=avatar.avatar_url if avatar else None,
            thumbnail_url=avatar.thumbnail_url if avatar else None,
            joined_at=profile.created_at,
            is_online=presence.is_online if presence else None,
            last_seen=presence.last_seen if presence else None,
            statistics=StatisticsResponse.of(statistics),
        )
