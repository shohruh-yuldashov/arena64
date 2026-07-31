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
from app.modules.users.public.dtos import UserRead, UserSummary


def to_user_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        username=user.username.value,
        email=user.email.value,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
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
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )
