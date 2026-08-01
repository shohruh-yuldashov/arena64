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
from app.modules.users.public import PrivacySettingsView, VisibilityLevel

#: Every settable key, including the three deprecated booleans. The `null`
#: rejection below walks this list, so a field added to the request schema
#: and not to this tuple would silently accept `null`.
_SETTINGS = (
    "show_country",
    "show_statistics",
    "last_seen",
    "online_status",
    "activity",
    "show_last_seen",
    "show_online_status",
    "show_activity",
)

#: The three settings a client may send in either of two spellings —
#: A64-013.2's audience field, or the boolean it replaced.
#:
#: Sending both for the same setting is a `422` rather than a precedence
#: rule. Any precedence rule is a coin flip from the client's point of view:
#: `{"last_seen": "friends", "show_last_seen": true}` is a request with two
#: incompatible intentions, and silently honouring one would leave the
#: caller believing it had set the other. On a privacy endpoint that is the
#: failure mode with real consequences.
_AUDIENCE_ALIASES = (
    ("last_seen", "show_last_seen"),
    ("online_status", "show_online_status"),
    ("activity", "show_activity"),
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
    last_seen: VisibilityLevel | None = Field(
        default=None,
        description=(
            "Who may see the time you were last online. **Default: `nobody`** — the "
            "only one of the five that is closed by default. A last-seen time "
            "published continuously reveals a sleep schedule and a working pattern, "
            "and unlike the others it is observed rather than declared.\n\n"
            "`friends` is accepted and stored, and currently behaves as `nobody`: "
            "friendships do not exist until A64-013.3, so no viewer can be a friend "
            "yet. That is the safe direction, and it is stated rather than silently "
            "rejected so a client can set the value it means today."
        ),
        examples=["nobody"],
    )
    show_last_seen: bool | None = Field(
        default=None,
        deprecated=True,
        description=(
            "**Deprecated — send `last_seen` instead.** Accepted for clients written "
            "before A64-013.2: `true` sets `everyone`, `false` sets `nobody`. Sending "
            "both this and `last_seen` is a `422`, because two incompatible "
            "intentions in one request must not be resolved by a coin flip."
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
    online_status: VisibilityLevel | None = Field(
        default=None,
        description=(
            "Who may see that you are online right now. Stored and honoured, but "
            "nothing publishes presence yet — the setting is here so it is already "
            "yours when that ships. Default: `everyone`."
        ),
        examples=["everyone"],
    )
    show_online_status: bool | None = Field(
        default=None,
        deprecated=True,
        description="**Deprecated — send `online_status` instead.** See `show_last_seen`.",
        examples=[True],
    )
    activity: VisibilityLevel | None = Field(
        default=None,
        description=(
            "Who may see your recent activity — match history, activity feed. Stored "
            "and honoured ahead of the feature it governs, as `online_status` is. "
            "Default: `everyone`."
        ),
        examples=["everyone"],
    )
    show_activity: bool | None = Field(
        default=None,
        deprecated=True,
        description="**Deprecated — send `activity` instead.** See `show_last_seen`.",
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
        for setting in _SETTINGS:
            if setting in sent and getattr(self, setting) is None:
                raise ValueError(f"{setting} cannot be null; send a value, or omit it")
        return self

    @model_validator(mode="after")
    def _reject_conflicting_spellings(self) -> "PrivacySettingsUpdateRequest":
        """One setting, one spelling per request — A64-013.2.

        A body carrying both `last_seen` and `show_last_seen` states two
        intentions for one column, and there is no correct way to pick
        between them. Precedence would be a coin flip from the client's
        side; on a privacy endpoint that means a caller believing it hid
        something it published.
        """
        sent = self.model_fields_set
        for audience, legacy in _AUDIENCE_ALIASES:
            if audience in sent and legacy in sent:
                raise ValueError(
                    f"send either {audience} or {legacy}, not both — "
                    f"{legacy} is deprecated and {audience} replaces it"
                )
        return self

    def resolved(self, audience: str, legacy: str) -> VisibilityLevel | None:
        """The level a caller asked for, whichever spelling they used.

        `None` when neither key was sent, which the router turns into
        `UNSET` — the partial-update signal. Living on the schema rather
        than in the router because the pairing of the two field names is
        this shape's business, and the router should not have to know that
        a legacy spelling exists at all.

        The two validators above have already ruled out `null` and the
        both-sent case, so this is total: at most one of the two is set,
        and if it is the boolean it widens through the same `of` the
        migration used.
        """
        sent = self.model_fields_set
        if audience in sent:
            level: VisibilityLevel | None = getattr(self, audience)
            return level
        if legacy in sent:
            return VisibilityLevel.of(visible=bool(getattr(self, legacy)))
        return None


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
    last_seen: VisibilityLevel = Field(
        description=(
            "Who may see the time you were last online, as `last_seen` on your public "
            "profile. `nobody` unless you changed it — the one setting here that is "
            "closed by default, because a published timestamp is a sleep schedule "
            "while 'online now' is momentary."
        ),
        examples=["nobody"],
    )
    show_last_seen: bool = Field(
        deprecated=True,
        description=(
            "**Deprecated — read `last_seen` instead.** `true` only when `last_seen` "
            "is `everyone`; a friends-only setting reads as `false` here, because "
            "this field asks whether *anybody* may see it. Retained so clients "
            "written before A64-013.2 keep working, and removed no earlier than the "
            "next API version."
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
    online_status: VisibilityLevel = Field(
        description=(
            "Who may see that you are online, as `is_online` on your public profile. "
            "Governs the indicator only — `last_seen` governs the timestamp beside "
            "it, separately."
        ),
        examples=["everyone"],
    )
    show_online_status: bool = Field(
        deprecated=True,
        description="**Deprecated — read `online_status` instead.** See `show_last_seen`.",
        examples=[True],
    )
    activity: VisibilityLevel = Field(
        description=(
            "Who may see your recent activity. **Stored but not yet applied "
            "anywhere** — no endpoint publishes activity, so changing this has no "
            "visible effect until a match history exists. Settable now so that the "
            "release which adds one is not also the release that decides who may "
            "read it."
        ),
        examples=["everyone"],
    )
    show_activity: bool = Field(
        deprecated=True,
        description="**Deprecated — read `activity` instead.** See `show_last_seen`.",
        examples=[True],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "show_country": True,
                    "show_statistics": True,
                    # The audience-valued settings, at the platform
                    # defaults. `last_seen` is the one that is closed out of
                    # the box.
                    "last_seen": "nobody",
                    "online_status": "everyone",
                    "activity": "everyone",
                    # The deprecated booleans, derived from the three above
                    # and shown so a client reading the old fields sees what
                    # they now report.
                    "show_last_seen": False,
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
            show_statistics=settings.show_statistics,
            # The audience-valued settings, and the deprecated booleans
            # **derived** from them. Deriving rather than storing both is
            # what keeps the two from disagreeing: `is_public` is the single
            # definition of "does the boolean say true", and a client
            # reading the old field sees `false` for a friends-only setting
            # — which is the honest answer to the question that field asks.
            last_seen=settings.last_seen,
            online_status=settings.online_status,
            activity=settings.activity,
            show_last_seen=settings.last_seen.is_public,
            show_online_status=settings.online_status.is_public,
            show_activity=settings.activity.is_public,
        )
