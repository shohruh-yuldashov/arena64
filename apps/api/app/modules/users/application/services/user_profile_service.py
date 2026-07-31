"""`UserProfileService` — the implementation behind
`users.public.ports.UserProfileReader`.

The same shape as `UserAccountService` and `UserCredentialService`, and
for the same reason: a thin translation between `UserService`'s domain
types and the published DTO, with no rule of its own. See
`user_account_service.py` on why that translation is not skippable — in
short, `User` is mutable and private, and handing one across a module
boundary would let a consumer change state `users` is responsible for.
"""

from uuid import UUID

from app.modules.users.application.mappers import to_user_read
from app.modules.users.application.services.user_service import UserService
from app.modules.users.public.dtos import UserRead


class UserProfileService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def get_profile(self, user_id: UUID) -> UserRead:
        return to_user_read(await self._users.get_user(user_id))
