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
    """The stranger's view — A64-012.1.

    Field by field like the two above, and here the discipline stops being
    a style preference and becomes the control: this is the mapping that
    would leak an email address if it were written as
    `PublicUserProfile.model_validate(user)` and the DTO later gained a
    field. Naming every field means adding one to `User` can never publish
    it to anonymous callers by accident.

    `bio` and `country` unwrap their value objects to plain strings, and
    `None` stays `None` — absence is one state, not an empty string.
    """
    return PublicUserProfile(
        id=user.id,
        username=user.username.value,
        display_name=_display_name_of(user),
        avatar=to_avatar_reference(user),
        country=user.country.value if user.country else None,
        preferred_language=user.preferred_language,
        bio=user.bio.value if user.bio else None,
        created_at=user.created_at,
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
