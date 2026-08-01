"""HTTP routes for your own profile — editing it (A64-012.3), controlling
who sees it (A64-012.4) and setting your own preferences (A64-012.5).

Six endpoints in three pairs, and **no business logic in any of them**. Each translates a
request into a service call and the result into a wire schema. Validation
is the domain's, the transaction is `users`', and URL composition is
`AvatarLinkBuilder`'s.

**No privacy logic either**, which is A64-012.4's architectural
requirement stated as a property of this file: "endpoints must not manually
hide fields". There is no `if not privacy.show_country` here and there
could not usefully be one — by the time a public profile reaches a route it
has already been redacted by `users`' mapper and composed by
`ProfileService`. The two endpoints below *set* the flags; nothing here
*applies* them.

## Why `/profile` and not `/profiles/{username}`

(The argument below is about editing, and it is the same argument that
makes `/profile/privacy` safe: the account is the token's, so "only the
owner may change their privacy settings" needs no check because no other
account is addressable.)

Singular, and with no identifier in it — the same design as
`/profile/avatar` (A64-012.2), for the same reason.

A64-012.3 requires that "only the profile owner may edit". The strongest
way to enforce that is an endpoint on which the target *cannot be
expressed*: the account comes from the access token's `sub`, and there is
no path segment, query parameter or body field that could name a different
one. There is no ownership check in this file because there is nothing to
check — another player's profile is not addressable here.

`/profiles/{username}` stays what it is: the public, unauthenticated read
of anybody's profile (A64-012.1). One is "me", one is "them", and keeping
them on separate paths is what stops the second from ever growing a write
verb.

## Errors need no handling here

Every failure is a typed exception on the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk. There is not one
`try`/`except` in this file:

    InvalidDisplayName  -> 422  validation_error
    InvalidBio          -> 422  validation_error
    InvalidCountryCode  -> 422  validation_error
    InvalidLanguage     -> 422  validation_error
    InvalidTimezone     -> 422  validation_error
    UserNotFound        -> 404  not_found
    MissingToken        -> 401  authentication_required

## What this endpoint deliberately cannot change

`username`, `email`, `is_active`, `is_verified` and the avatar. The first
two have their own flows for good reasons recorded on
`UpdateUserProfile`; the next two are state transitions rather than
values; the avatar is `/profile/avatar`'s, because changing it means
validating and re-encoding an image rather than storing a string.
"""

import logging

from fastapi import APIRouter, Depends, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.core.sentinels import UNSET
from app.modules.auth.presentation.dependencies import CurrentUser
from app.modules.avatars.presentation.dependencies import AvatarLinkBuilderDep
from app.modules.profiles.presentation.dependencies import (
    PreferencesEditorDep,
    PrivacySettingsEditorDep,
    ProfileEditorDep,
    ProfileServiceDep,
)
from app.modules.profiles.presentation.rate_limits import (
    enforce_preferences_update_limit,
    enforce_privacy_update_limit,
)
from app.modules.profiles.presentation.schemas import (
    GameplayPreferencesUpdate,
    LocalePreferencesUpdate,
    MyProfileResponse,
    PreferencesResponse,
    PreferencesUpdateRequest,
    PrivacySettingsResponse,
    PrivacySettingsUpdateRequest,
    ProfileUpdateRequest,
)
from app.modules.users.public import (
    GameplayEdits,
    LocaleEdits,
    PreferenceEdits,
    PrivacyEdits,
    ProfileEdits,
)

logger = logging.getLogger(__name__)

my_profile_router = APIRouter(prefix="/profile", tags=["profile"])


_UNAUTHORIZED: Responses = error_response(
    401,
    "No access token was presented, or it was invalid or expired.",
)
_NOT_FOUND: Responses = error_response(
    404,
    "The account no longer exists — deleted while a valid token was in flight.",
)
_UNPROCESSABLE: Responses = error_response(
    422,
    (
        "A field failed validation, or the body carried a field this endpoint does "
        "not accept. `message` names which. Unknown fields are **rejected**, not "
        "ignored — a silently dropped `username` would look like a successful "
        "rename."
    ),
)


@my_profile_router.get(
    "/me",
    status_code=status.HTTP_200_OK,
    summary="Read your own profile",
    response_description="Your profile, including fields the public view omits.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def get_my_profile(
    user: CurrentUser,
    editor: ProfileEditorDep,
    profiles: ProfileServiceDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[MyProfileResponse]:
    """Returns the authenticated account's own profile.

    Differs from `GET /profiles/{username}` in two ways that matter. It is
    **never redacted** — statistics and presence are reported whatever the
    privacy flags say, because those govern strangers rather than you; and
    it is scoped to the caller by construction, so it needs no username and
    cannot be pointed at anybody else.

    Differs from `GET /auth/me` in the other direction: that one answers
    *who am I* and carries the email and verification state; this one
    answers *what does my profile say* and carries the three editable
    fields. Together they are the two halves an account settings page
    needs.

    **No `timezone` and no `language`.** Both were here until A64-012.5
    moved them to `GET /profile/preferences`, which is now the only place
    either is read or written on this router.

    Carries your **statistics** and your **presence**, always —
    `show_statistics`, `show_online_status` and `show_last_seen` govern what
    a stranger sees on `GET /profiles/{username}`, never what you see of
    yourself (A64-012.6, A64-012.7). A control that hid a record from the
    person who hid it would be one nobody could verify they had set.

    `is_online` and `last_seen` are `null` for every account today: presence
    is written by the realtime gateway, which does not exist yet. A `null`
    here means unobserved, never hidden.

    Returns exactly the shape `PATCH /profile` returns, so a client can
    populate an edit form and render the result of a save with one parser.
    """
    profile = await editor.get_own_profile(user.id)
    statistics = await profiles.get_own_statistics(user.id)
    presence = await profiles.get_own_presence(user.id)

    return build_response(
        MyProfileResponse.of(profile, avatar_links.links_for(profile.avatar), statistics, presence)
    )


@my_profile_router.patch(
    "",
    status_code=status.HTTP_200_OK,
    summary="Update your own profile",
    response_description="The profile as it now stands, with normalised values.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_UNPROCESSABLE},
)
async def update_my_profile(
    payload: ProfileUpdateRequest,
    user: CurrentUser,
    editor: ProfileEditorDep,
    profiles: ProfileServiceDep,
    avatar_links: AvatarLinkBuilderDep,
) -> ApiResponse[MyProfileResponse]:
    """Applies a partial update to the authenticated account's profile.

    **A real PATCH.** Send only what changes; omitted fields are left
    alone. All three fields — `display_name`, `bio`, `country` —
    distinguish *omitted* from *explicitly null*: omitting leaves the value
    as it is, sending `null` clears it.

    **Only your own profile.** The account comes from your access token;
    there is no way to name a different one.

    **Unknown fields are rejected.** A body carrying `username`,
    `is_verified` or `avatar_object_key` returns `422` naming the field.
    None of them is settable here, and failing loudly is what stops a
    client from believing a rename succeeded.

    ## Validation

    | Field | Rule |
    | --- | --- |
    | `display_name` | 3-50 characters, any script, trimmed |
    | `bio` | at most 500 characters, plain text |
    | `country` | an assigned ISO 3166-1 alpha-2 code |

    `preferred_language` and `timezone` were editable here until A64-012.5
    moved them to `PATCH /profile/preferences`. Sending either now returns
    a `422` naming it, which is the loud answer a silently-dropped setting
    would not give.

    `display_name` and `bio` additionally reject control and
    bidirectional characters — a right-to-left override in a name rendered
    beside every match reverses the text around it, which is a display
    spoofing primitive rather than a typo.

    **Nothing is written if any field is rejected.** Values are validated
    as they are converted, before the account row is touched, so a request
    with a good bio and a bad country changes neither.

    Returns the full profile with **normalised** values — a padded display
    name comes back trimmed, `uz` comes back `UZ` — so a client renders
    what was actually stored rather than what it sent.
    """
    # `model_fields_set` is what makes this a real PATCH: it reports which
    # keys the client actually sent, so an omitted field maps to `UNSET`
    # (leave alone) while an explicit `null` maps to `None` (clear).
    # Reading the attribute values alone cannot tell the two apart, and
    # treating them the same is how a PATCH silently wipes fields the
    # caller never mentioned.
    sent = payload.model_fields_set
    edits = ProfileEdits(
        display_name=payload.display_name if "display_name" in sent else UNSET,
        bio=payload.bio if "bio" in sent else UNSET,
        country=payload.country if "country" in sent else UNSET,
    )

    profile = await editor.update_own_profile(user.id, edits)

    # **Field names only — never values, old or new.** A64-012.3 requires
    # the updated fields to be logged and forbids logging previous values
    # of sensitive fields, and the safe reading of that is to log no
    # values at all: a display name and a biography are personal data
    # (§14.1), a country is location data, and none of them is worth
    # putting in a system with broader read access and different retention
    # than the database (services.md §8.5). The field list answers what an
    # audit actually asks — *what changed, for whom, when* — and the
    # database holds the values.
    logger.info(
        "profile_updated",
        extra={"user_id": str(user.id), "updated_fields": sorted(sent)},
    )

    # Read after the write, so the response is one coherent view of the
    # account rather than an edited profile beside a record fetched before
    # it. Nothing an edit can change touches statistics or presence today,
    # and reading them here means that stays true for free if something ever
    # does.
    statistics = await profiles.get_own_statistics(user.id)
    presence = await profiles.get_own_presence(user.id)

    return build_response(
        MyProfileResponse.of(profile, avatar_links.links_for(profile.avatar), statistics, presence)
    )


# --- privacy settings (A64-012.4) -------------------------------------------
#
# Two endpoints on `/profile/privacy`, kept out of `PATCH /profile` above on
# purpose. Merging them would look tidier and would be wrong in three ways:
# the write needs a rate limit the profile edit does not have, the two go
# through different published ports so that editing a biography does not
# confer the ability to publish an account's activity, and a settings screen
# loads visibility without touching a display name.

_PRIVACY_UNPROCESSABLE: Responses = error_response(
    422,
    (
        "The body carried a field this endpoint does not accept, or a flag was "
        "sent as `null`. `message` names which. Unknown fields are **rejected**, "
        "not ignored — a client that believed it had hidden something it had not "
        "would act as though the field were private."
    ),
)
_TOO_MANY_REQUESTS: Responses = error_response(
    429,
    (
        "Too many privacy updates from this account. Counted **per user**, not per "
        "network address, so a shared connection is never somebody else's problem. "
        "`Retry-After` says how long to wait."
    ),
)


@my_profile_router.get(
    "/privacy",
    status_code=status.HTTP_200_OK,
    summary="Read your privacy settings",
    response_description="All five flags, as they currently stand.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def get_my_privacy_settings(
    user: CurrentUser,
    privacy: PrivacySettingsEditorDep,
) -> ApiResponse[PrivacySettingsResponse]:
    """Returns the authenticated account's privacy settings.

    Always all five flags, never a subset. An account has an answer for
    every one of them — the columns are `NOT NULL` and the platform
    defaults apply to rows nobody has touched — so a client renders a
    settings screen from this response alone and never has to guess.

    Deliberately **not** rate limited: it is one indexed read of a row you
    have already authenticated as, it changes nothing, and a settings
    screen loads it on every visit. See `rate_limits.py`.

    Scoped to you by construction. There is no path segment or parameter
    that could name another account, which is why there is no ownership
    check — and why no endpoint anywhere returns *somebody else's* flags.
    A stranger's profile shows a `null` where a hidden field would be and
    says nothing about why.
    """
    return build_response(PrivacySettingsResponse.of(await privacy.get_privacy_settings(user.id)))


@my_profile_router.patch(
    "/privacy",
    status_code=status.HTTP_200_OK,
    summary="Update your privacy settings",
    response_description="All five flags as they now stand.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND, **_PRIVACY_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
    dependencies=[Depends(enforce_privacy_update_limit)],
)
async def update_my_privacy_settings(
    payload: PrivacySettingsUpdateRequest,
    user: CurrentUser,
    privacy: PrivacySettingsEditorDep,
) -> ApiResponse[PrivacySettingsResponse]:
    """Applies a partial update to the authenticated account's privacy
    settings.

    **A real PATCH.** Send only the flags you are changing; the rest are
    left exactly as they were. `{"show_country": false}` does not reset the
    other four to their defaults — which matters most for `show_last_seen`,
    whose default is *off*, so a reset would publish something the player
    had deliberately kept private.

    **Only your own settings.** The account comes from your access token;
    there is no way to name a different one.

    **Unknown fields are rejected**, and a flag sent as `null` is rejected
    too. A boolean control has two states, so `null` has no meaning here
    that would not have to be invented — and the most likely invention,
    "reset to default", is the one thing this endpoint must not do
    implicitly.

    ## What each flag governs

    | Flag | Effect on your public profile | Default |
    | --- | --- | --- |
    | `show_country` | `country` is `null` when off | `true` |
    | `show_last_seen` | `last_seen` is `null` when off | **`false`** |
    | `show_statistics` | `statistics` is `null` when off — never zeroes | `true` |
    | `show_online_status` | live presence, once it exists | `true` |
    | `show_activity` | recent activity, once it exists | `true` |

    A hidden field is `null` and nothing else: no placeholder, no marker,
    and nothing that distinguishes it from a field you simply never filled
    in. That is deliberate — reporting *that* something is hidden answers
    the question hiding it was meant to decline.

    **Ratings are always public** and no flag covers them. A rating is what
    pairing is computed from and what leaderboards publish; `show_statistics`
    covers the record of games behind it, not the number itself.

    Rate limited per network address. See the `429` response.
    """
    # `model_fields_set` is what makes this a real PATCH — the same
    # mechanism `PATCH /profile` uses above. Reading the attribute values
    # alone cannot distinguish `false` from "not sent", and on a privacy
    # endpoint that confusion writes `false` over a flag the caller never
    # mentioned.
    #
    # The `is not None` half is belt and braces: the schema has already
    # rejected an explicit null, so a key in `sent` carries a real boolean
    # and this branch is unreachable. It stays because `PrivacyEdits` has
    # no `None` in its unions — a validator removed or reordered later
    # would otherwise put a `None` where the type says a `bool` is, which
    # the type checker catches here and would not catch at all if this were
    # written as a `cast`.
    sent = payload.model_fields_set
    edits = PrivacyEdits(
        show_country=(
            payload.show_country
            if "show_country" in sent and payload.show_country is not None
            else UNSET
        ),
        show_last_seen=(
            payload.show_last_seen
            if "show_last_seen" in sent and payload.show_last_seen is not None
            else UNSET
        ),
        show_statistics=(
            payload.show_statistics
            if "show_statistics" in sent and payload.show_statistics is not None
            else UNSET
        ),
        show_online_status=(
            payload.show_online_status
            if "show_online_status" in sent and payload.show_online_status is not None
            else UNSET
        ),
        show_activity=(
            payload.show_activity
            if "show_activity" in sent and payload.show_activity is not None
            else UNSET
        ),
    )

    settings = await privacy.update_privacy_settings(user.id, edits)

    # **Field names only — never values, old or new.** A64-012.4 requires
    # the updated fields to be logged and forbids logging previous values,
    # and the same reading applies here as on `profile_updated`: log no
    # values at all. Which parts of their account a person has chosen to
    # hide is itself sensitive — a log line saying `show_last_seen: false`
    # records a privacy decision in a system with broader read access and
    # different retention than the database (services.md §8.5). The field
    # list answers what an audit asks — *what changed, for whom, when* —
    # and the row holds the answers.
    logger.info(
        "privacy_updated",
        extra={"user_id": str(user.id), "updated_fields": sorted(sent)},
    )

    return build_response(PrivacySettingsResponse.of(settings))


# --- preferences (A64-012.5) ------------------------------------------------
#
# Two endpoints on `/profile/preferences`, and the third pair on this
# router. The split from `/profile` and `/profile/privacy` is the same one
# those two make from each other: a profile edit changes what a player says
# about themselves, a privacy flag changes what strangers see, and a
# preference changes what the player themselves sees. Three screens, three
# ports, three rate-limit policies.

_PREFERENCES_UNPROCESSABLE: Responses = error_response(
    422,
    (
        "An unknown preference group or key, a value outside its allowed set, a "
        "timezone this system does not know, or a key sent as `null`. `message` "
        "names which. Unknown keys are **rejected** at both levels — an unknown "
        "group and an unknown setting inside a known group are both errors."
    ),
)
_PREFERENCES_TOO_MANY: Responses = error_response(
    429,
    (
        "Too many preference updates from this account. Counted **per user**, not "
        "per network address, so a shared connection is never somebody else's "
        "problem. `Retry-After` says how long to wait."
    ),
)


@my_profile_router.get(
    "/preferences",
    status_code=status.HTTP_200_OK,
    summary="Read your preferences",
    response_description="Every preference group, complete.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def get_my_preferences(
    user: CurrentUser,
    preferences: PreferencesEditorDep,
) -> ApiResponse[PreferencesResponse]:
    """Returns the authenticated account's personal settings.

    **Always complete.** Every group is present and every setting inside it
    has a value, even for an account that has never opened a settings
    screen — the stored document is empty for such an account and the
    domain fills each absent key with its default. So a client renders
    every control from this response and never has to carry its own copy of
    the defaults, which is the copy that would drift.

    Not rate limited: one indexed read of a row you have already
    authenticated as, changing nothing, loaded on every visit to a settings
    screen. See `rate_limits.py`.

    **Never public.** No anonymous endpoint returns any of this — a board
    theme and a timezone are yours, and `GET /profiles/{username}` carries
    neither.
    """
    return build_response(PreferencesResponse.of(await preferences.get_preferences(user.id)))


@my_profile_router.patch(
    "/preferences",
    status_code=status.HTTP_200_OK,
    summary="Update your preferences",
    response_description="Every preference group as it now stands.",
    responses={
        **_UNAUTHORIZED,
        **_NOT_FOUND,
        **_PREFERENCES_UNPROCESSABLE,
        **_PREFERENCES_TOO_MANY,
    },
    dependencies=[Depends(enforce_preferences_update_limit)],
)
async def update_my_preferences(
    payload: PreferencesUpdateRequest,
    user: CurrentUser,
    preferences: PreferencesEditorDep,
) -> ApiResponse[PreferencesResponse]:
    """Applies a partial update to the authenticated account's preferences.

    **A real PATCH, at two levels.** An omitted *group* is left entirely
    alone, and inside a group an omitted *setting* is left alone. So
    `{"locale": {"timezone": "UTC"}}` changes a timezone and touches
    neither the language beside it nor any of the five gameplay settings.

    **Only your own preferences.** The account comes from your access
    token; there is no way to name a different one.

    **Unknown keys are rejected at both levels.** `{"ui": {...}}` is a
    `422` naming `ui`; `{"gameplay": {"sound": true}}` is a `422` naming
    `sound`. A `null` value is rejected too — no preference has an empty
    state, so `null` would need a meaning invented for it, and the likely
    invention ("reset to default") is not a decision this endpoint should
    make on a client's behalf.

    ## Groups and settings

    | Group | Setting | Values | Default |
    | --- | --- | --- | --- |
    | `gameplay` | `board_theme` | `classic`, `wood`, `marble`, `midnight` | `classic` |
    | `gameplay` | `piece_set` | `classic`, `modern`, `neo` | `classic` |
    | `gameplay` | `confirm_move` | boolean | `false` |
    | `gameplay` | `show_coordinates` | boolean | `true` |
    | `gameplay` | `animation_speed` | `instant`, `fast`, `normal`, `slow` | `normal` |
    | `locale` | `preferred_language` | `en`, `ru`, `uz` | `en` |
    | `locale` | `timezone` | any IANA name | `UTC` |

    `animation_speed: instant` disables motion rather than being a fourth
    speed — it is an accessibility setting, and motion is a migraine and
    vestibular trigger.

    **`preferred_language` and `timezone` are changed here and nowhere
    else.** Both were editable through `PATCH /profile` before A64-012.5,
    which removed them there; a field with two writable endpoints is a
    field whose validation and audit trail depend on which one a client
    happened to use.

    **Nothing is written if any value is rejected.** The timezone is
    validated before the account row is touched, so a request with a good
    board theme and a bad timezone changes neither.

    Rate limited **per account** rather than per network address, so a
    shared office or carrier connection never throttles one player for
    another's behaviour. See the `429` response.
    """
    # The two-level `model_fields_set` walk. The outer level distinguishes
    # "did not mention gameplay" from "sent an empty gameplay object"; the
    # inner one distinguishes a setting left alone from one being changed.
    # Collapsing either would turn a one-setting change into a group reset,
    # which is a failure a client cannot see and a player notices later.
    sent_groups = payload.model_fields_set
    edits = PreferenceEdits(
        gameplay=(
            _gameplay_edits(payload.gameplay)
            if "gameplay" in sent_groups and payload.gameplay is not None
            else UNSET
        ),
        locale=(
            _locale_edits(payload.locale)
            if "locale" in sent_groups and payload.locale is not None
            else UNSET
        ),
    )

    updated = await preferences.update_preferences(user.id, edits)

    # **Group names only — never values, and never the settings inside.**
    # A64-012.5 asks for the updated *groups*, which is a narrower record
    # than `profile_updated`'s field list and deliberately so: a board
    # theme is harmless but a timezone is location data (services.md §8.5),
    # and "this account changed something in its locale group" answers what
    # an audit asks without putting a person's region in a log with broader
    # read access than the database.
    logger.info(
        "preferences_updated",
        extra={"user_id": str(user.id), "updated_groups": sorted(sent_groups)},
    )

    return build_response(PreferencesResponse.of(updated))


def _gameplay_edits(payload: GameplayPreferencesUpdate) -> GameplayEdits:
    """One group's `model_fields_set` walk.

    A helper per group rather than ten inline ternaries in the handler, so
    that adding `ui` is a third function beside these two rather than five
    more lines inside a route. The schema has already rejected an explicit
    `null`, so a key present in `sent` carries a real value — the
    `is not None` half is what keeps that a type-checked fact rather than
    an assumption (see `update_my_privacy_settings` for the same note).
    """
    sent = payload.model_fields_set
    return GameplayEdits(
        board_theme=(
            payload.board_theme
            if "board_theme" in sent and payload.board_theme is not None
            else UNSET
        ),
        piece_set=(
            payload.piece_set if "piece_set" in sent and payload.piece_set is not None else UNSET
        ),
        confirm_move=(
            payload.confirm_move
            if "confirm_move" in sent and payload.confirm_move is not None
            else UNSET
        ),
        show_coordinates=(
            payload.show_coordinates
            if "show_coordinates" in sent and payload.show_coordinates is not None
            else UNSET
        ),
        animation_speed=(
            payload.animation_speed
            if "animation_speed" in sent and payload.animation_speed is not None
            else UNSET
        ),
    )


def _locale_edits(payload: LocalePreferencesUpdate) -> LocaleEdits:
    """The locale group's `model_fields_set` walk — see `_gameplay_edits`."""
    sent = payload.model_fields_set
    return LocaleEdits(
        preferred_language=(
            payload.preferred_language
            if "preferred_language" in sent and payload.preferred_language is not None
            else UNSET
        ),
        timezone=(
            payload.timezone if "timezone" in sent and payload.timezone is not None else UNSET
        ),
    )
