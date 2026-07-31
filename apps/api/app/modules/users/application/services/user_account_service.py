"""`UserAccountService` — the implementation behind
`users.public.ports.UserAccountCreator`.

A deliberately thin adapter over `UserService`, and thin is the point.
It exists to do exactly two things `UserService` must not:

  **Return a published DTO rather than the domain entity.** `UserService`
  returns `User`, which is mutable and is this module's private
  representation; handing it across a module boundary would let `auth`
  mutate state `users` is responsible for, and would couple `auth` to an
  internal shape (R-1). The port returns `UserRead` instead.

  **Accept the port's structural input.** `auth` passes its own
  `RegisterUser` command; this translates it into the `CreateUser` command
  `UserService` expects, so neither module imports the other's command
  type.

Everything else — validation, uniqueness, the transaction — is
`UserService`'s, unchanged. A second implementation of the rules here
would be the divergence this adapter exists to avoid.
"""

from app.modules.users.application.commands import CreateUser
from app.modules.users.application.mappers import to_user_read
from app.modules.users.application.services.user_service import UserService
from app.modules.users.public.dtos import UserRead
from app.modules.users.public.ports import NewUserAccount


class UserAccountService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def create(self, account: NewUserAccount) -> UserRead:
        created = await self._users.create_user(
            CreateUser(
                username=account.username,
                email=account.email,
                password_hash=account.password_hash,
                preferred_language=account.preferred_language,
                timezone=account.timezone,
                display_name=account.display_name,
            )
        )
        return to_user_read(created)
