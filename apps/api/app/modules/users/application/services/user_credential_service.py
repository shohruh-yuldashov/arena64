"""`UserCredentialService` — the implementation behind
`users.public.ports.UserCredentialStore`.

The same shape as `UserAccountService`, for the same reason: a thin
translation between `UserService`'s domain types and the published DTOs,
with no rule of its own. See that module's docstring for why the
translation is not skippable.

What is specific to this one is the *shape* of the read. It returns
`UserCredentials` — the account view and the stored hash together, from
the single lookup `UserService` already performs — rather than exposing
`User`, which carries every field this module owns and would hand `auth`
a mutable copy of state it has no business changing.
"""

from uuid import UUID

from app.modules.users.application.mappers import to_user_read
from app.modules.users.application.services.user_service import UserService
from app.modules.users.public.credentials import UserCredentials


class UserCredentialService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def find_credentials_by_email(self, email: str) -> UserCredentials | None:
        user = await self._users.lookup_by_email(email)
        if user is None:
            # Returns, never raises — the port's docstring explains why
            # this branch must be as cheap and as quiet as the other.
            return None

        return UserCredentials(
            account=to_user_read(user),
            password_hash=user.password_hash,
            locked_until=user.locked_until,
        )

    async def replace_password_hash(
        self,
        user_id: UUID,
        *,
        expected_hash: str,
        new_hash: str,
    ) -> bool:
        return await self._users.replace_password_hash(
            user_id, expected_hash=expected_hash, new_hash=new_hash
        )
