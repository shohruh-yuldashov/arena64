"""Domain entity -> published DTO.

One definition, two callers: this module's own router renders `UserRead`
for `GET /users/{id}`, and `UserAccountService` returns the same shape
across the module boundary to `auth`. A64-010 had this mapping private to
the router; a second caller makes it duplication, and a `UserRead` built
two slightly different ways is precisely how a field ends up present on
one path and missing on the other.

Written out field by field rather than `UserRead.model_validate(user)`
because the entity's `username`, `email` and `timezone` are value objects,
not strings — an implicit conversion would either fail or serialise the
wrapper. Being explicit also means adding a field to the entity never
leaks it onto the API by accident, which for an entity carrying a password
hash is worth the extra lines.
"""

from app.modules.users.domain.entities import User
from app.modules.users.public.dtos import (
    AvatarReference,
    OwnUserProfile,
    PrivacySettingsView,
    ProfileVisibility,
    PublicUserProfile,
    UserRead,
    UserSummary,
)


def to_user_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        username=user.username.value,
        email=user.email.value,
        display_name=_display_name_of(user),
        bio=user.bio.value if user.bio else None,
        country=user.country.value if user.country else None,
        avatar=to_avatar_reference(user),
        preferred_language=user.preferred_language,
        timezone=user.timezone.value,
        is_active=user.is_active,
        is_verified=user.is_verified,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def to_user_summary(user: User) -> UserSummary:
    return UserSummary(
        id=user.id,
        username=user.username.value,
        display_name=_display_name_of(user),
        avatar=to_avatar_reference(user),
    )


def to_public_profile(user: User) -> PublicUserProfile:
    """The stranger's view — A64-012.1, redacted by A64-012.4.

    Field by field like the two above, and here the discipline stops being
    a style preference and becomes the control: this is the mapping that
    would leak an email address if it were written as
    `PublicUserProfile.model_validate(user)` and the DTO later gained a
    field. Naming every field means adding one to `User` can never publish
    it to anonymous callers by accident.

    `bio` and `country` unwrap their value objects to plain strings, and
    `None` stays `None` — absence is one state, not an empty string.

    ## This function is where privacy is enforced for `users`' own fields

    UP-4 requires privacy preferences to be enforced server-side on every
    read path, and this is the single read path by which a stranger obtains
    anything `users` owns — `PublicProfileReader` has no other method, and
    nothing outside this module can construct a `PublicUserProfile`.

    So `show_country` is applied here rather than published. A hidden
    country is `None`, which is byte-for-byte what a player who never set
    one returns: no flag, no placeholder, nothing for a caller to render
    differently and nothing for a scraper to learn. A64-012.4 asks for
    `country: null` and this is the form of it that cannot be undone
    downstream.

    The four flags that *are* published govern data this module does not
    hold — see `ProfileVisibility`.
    """
    return PublicUserProfile(
        id=user.id,
        username=user.username.value,
        display_name=_display_name_of(user),
        avatar=to_avatar_reference(user),
        # The redaction. Not `country if show else None` written at the
        # call site of some consumer — here, before the value crosses the
        # boundary, so there is no version of this DTO in existence that
        # carries a country the player hid.
        country=(
            user.country.value if user.privacy.show_country and user.country is not None else None
        ),
        preferred_language=user.preferred_language,
        bio=user.bio.value if user.bio else None,
        created_at=user.created_at,
        visibility=to_profile_visibility(user),
    )


def to_profile_visibility(user: User) -> ProfileVisibility:
    """The four decisions a consumer has to apply itself — A64-012.4.

    `show_country` is **absent by design**: `users` owns that column and
    redacts it in `to_public_profile` above, so publishing the flag would
    add a disclosure ("this player hides their country") without enabling
    anything. The four here govern statistics, presence and activity, none
    of which this module holds — it can decide, but it cannot redact.

    A helper beside the other `to_*` mappers rather than an inline literal,
    for the reason `to_avatar_reference` is one: a sixth privacy flag is
    wired in once, and the next read path that composes a public view gets
    it without knowing it was added.
    """
    privacy = user.privacy
    return ProfileVisibility(
        last_seen=privacy.show_last_seen,
        statistics=privacy.show_statistics,
        online_status=privacy.show_online_status,
        activity=privacy.show_activity,
    )


def to_privacy_settings(user: User) -> PrivacySettingsView:
    """The owner's own controls — A64-012.4.

    Unredacted, and that is the whole difference from
    `to_profile_visibility` above: an account holder is shown what they
    chose, including `show_country`, because a settings screen that hid a
    setting from the person who set it would be unusable.

    Explicit field by field like every other mapper here. The habit guards
    a specific mistake on this one: `PrivacySettings` is a domain value
    object and `PrivacySettingsView` a published DTO with the same five
    field names, so `model_validate` would appear to work and would keep
    appearing to work right up until the two diverge.
    """
    privacy = user.privacy
    return PrivacySettingsView(
        show_country=privacy.show_country,
        show_last_seen=privacy.show_last_seen,
        show_statistics=privacy.show_statistics,
        show_online_status=privacy.show_online_status,
        show_activity=privacy.show_activity,
    )


def to_avatar_reference(user: User) -> AvatarReference:
    """The three avatar columns as one published value.

    A helper rather than three inline fields at each call site, so that a
    fourth avatar column — a moderation flag, a content hash — is added
    once. Sits beside the other three `to_*` mappers because it has the
    same job and the same reason for being explicit: a field added to the
    entity never reaches a published DTO by accident.
    """
    return AvatarReference(
        object_key=user.avatar_object_key,
        version=user.avatar_version,
        uploaded_at=user.avatar_uploaded_at,
    )


def _display_name_of(user: User) -> str | None:
    """Unwraps the value object for the wire. `None` stays `None` —
    absence is one state, not an empty string."""
    return user.display_name.value if user.display_name else None


def to_own_profile(user: User) -> OwnUserProfile:
    """The account holder's own editable view — A64-012.3.

    Explicit field by field like the others. Here the discipline guards a
    specific mistake: this shape is returned from the *edit* endpoint, and
    a `model_validate(user)` would publish whatever the entity gains next —
    including `password_hash`, which sits on the same object.
    """
    return OwnUserProfile(
        id=user.id,
        username=user.username.value,
        display_name=_display_name_of(user),
        bio=user.bio.value if user.bio else None,
        country=user.country.value if user.country else None,
        preferred_language=user.preferred_language,
        timezone=user.timezone.value,
        avatar=to_avatar_reference(user),
        created_at=user.created_at,
    )
