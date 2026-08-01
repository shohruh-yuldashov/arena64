"""Wire schemas for the privacy settings endpoints — A64-012.4.

Two types: what a client may send to `PATCH /profile/privacy`, and what
both privacy endpoints return.

## Every flag is `show_*`, never `hide_*`

Five booleans that all mean the same thing in the same direction. A single
`hide_last_seen` among four `show_*` fields would be correct in isolation
and would eventually be applied backwards by somebody reading quickly — and
a privacy control applied backwards publishes exactly what it was asked to
conceal. The domain, the port, the DTO and the wire all use one spelling
for this reason.

## Unknown fields are rejected, not ignored

`extra="forbid"` from `BaseRequestDTO`, so `{"show_email": false}` returns
`422` naming the field rather than a `200` that quietly changed nothing.
A64-012.4 asks for exactly this ("reject unknown fields"), and on this
endpoint the argument is stronger than it was for profile editing: a client
that believes it has hidden something it has not is worse off than one that
got an error, because it will act as though the field is private.

## Why `null` is rejected for every flag

A boolean privacy control has two states and an account always has an
answer, so there is no "clear it" for `null` to mean. Accepting it would
force a third meaning to be invented — most likely "reset to default",
which for `show_last_seen` means turning something *back off* and for the
other four means turning something *on*. That is the last decision a
privacy API should make implicitly. Omit a flag to leave it alone.
"""

from pydantic import Field, model_validator

from app.core.dto import BaseRequestDTO, BaseResponseDTO
from app.modules.users.public import PrivacySettingsView

_FLAGS = (
    "show_country",
    "show_last_seen",
    "show_statistics",
    "show_online_status",
    "show_activity",
)


class PrivacySettingsUpdateRequest(BaseRequestDTO):
    """The `PATCH /profile/privacy` body. Every flag optional; send only
    what changes.

    Sending one flag does **not** reset the other four to their defaults.
    That is worth stating on the schema rather than only in the router,
    because it is the failure a client cannot detect: a request that
    silently re-enabled `show_last_seen` would look like a success and
    would publish a person's schedule.
    """

    show_country: bool | None = Field(
        default=None,
        description=(
            "Whether your country is shown on your public profile. Hidden means "
            "the field is `null` — indistinguishable from a player who never set "
            "one. Default: `true`."
        ),
        examples=[True],
    )
    show_last_seen: bool | None = Field(
        default=None,
        description=(
            "Whether the time you were last online is shown. **Default: `false`** "
            "— the only one of the five that is off by default. A last-seen time "
            "published continuously reveals a sleep schedule and a working "
            "pattern, and unlike the others it is observed rather than declared."
        ),
        examples=[False],
    )
    show_statistics: bool | None = Field(
        default=None,
        description=(
            "Whether your aggregate match record — games, wins, losses, draws, "
            "win rate — is shown. Hidden means `statistics` is `null`, never "
            "zeroes. **Does not cover your ratings**, which are always public: "
            "a rating is what pairing is computed from. Default: `true`."
        ),
        examples=[True],
    )
    show_online_status: bool | None = Field(
        default=None,
        description=(
            "Whether other players can see that you are online right now. Stored "
            "and honoured, but nothing publishes presence yet — the setting is "
            "here so it is already yours when that ships. Default: `true`."
        ),
        examples=[True],
    )
    show_activity: bool | None = Field(
        default=None,
        description=(
            "Whether your recent activity — match history, activity feed — is "
            "shown. Stored and honoured ahead of the feature it governs, as "
            "`show_online_status` is. Default: `true`."
        ),
        examples=[True],
    )

    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "examples": [
                {"show_last_seen": True},
                {
                    "show_country": False,
                    "show_last_seen": False,
                    "show_statistics": False,
                    "show_online_status": False,
                    "show_activity": False,
                },
            ]
        },
    }

    @model_validator(mode="after")
    def _reject_explicit_null(self) -> "PrivacySettingsUpdateRequest":
        """A flag sent as `null` is a client error, not a third state.

        Mirrors `ProfileUpdateRequest`'s validator for `preferred_language`
        and `timezone`, and applies to *every* field here rather than two
        of five: none of these has a cleared state. Silently ignoring a
        `null` would leave the caller believing a toggle applied.
        """
        sent = self.model_fields_set
        for flag in _FLAGS:
            if flag in sent and getattr(self, flag) is None:
                raise ValueError(f"{flag} cannot be null; send true or false, or omit it")
        return self


class PrivacySettingsResponse(BaseResponseDTO):
    """Your privacy settings, as returned by both privacy endpoints.

    One shape for `GET` and `PATCH`, because both answer the same question
    — *what are my settings now* — and returning the complete set from the
    PATCH means a client renders every toggle from the response rather than
    applying an optimistic update and drifting from what was stored.

    **Only ever returned to the account holder.** Nothing on the public
    profile carries these flags: `GET /profiles/{username}` shows a `null`
    where a hidden field would be and says nothing about why, because
    "this player hides their country" is itself the disclosure the setting
    declines to make.
    """

    show_country: bool = Field(
        description="Whether your country is shown on your public profile.",
        examples=[True],
    )
    show_last_seen: bool = Field(
        description=(
            "Whether the time you were last online is shown. `false` unless you turned it on."
        ),
        examples=[False],
    )
    show_statistics: bool = Field(
        description=(
            "Whether your aggregate match record is shown. Does not affect your "
            "ratings, which are always public."
        ),
        examples=[True],
    )
    show_online_status: bool = Field(
        description="Whether other players can see that you are online.",
        examples=[True],
    )
    show_activity: bool = Field(
        description="Whether your recent activity is shown.",
        examples=[True],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "show_country": True,
                    "show_last_seen": False,
                    "show_statistics": True,
                    "show_online_status": True,
                    "show_activity": True,
                }
            ]
        }
    }

    @classmethod
    def of(cls, settings: PrivacySettingsView) -> "PrivacySettingsResponse":
        """Renders the published owner view.

        Field by field rather than `model_validate(settings)`, for the
        reason `users.application.mappers` gives — and with a sharper edge
        here, because the DTO and this schema share all five field names.
        An implicit conversion would work today and would keep appearing to
        work on the day a sixth flag is added to one of them and not the
        other.
        """
        return cls(
            show_country=settings.show_country,
            show_last_seen=settings.show_last_seen,
            show_statistics=settings.show_statistics,
            show_online_status=settings.show_online_status,
            show_activity=settings.show_activity,
        )
