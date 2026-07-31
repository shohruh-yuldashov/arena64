"""`EmailVerificationWriter` — the implementation behind
`users.public.ports.EmailVerifier`.

The same shape as the other three published-port adapters, and for the
same reason: a thin translation between `UserService`'s domain types and
the published DTO, with no rule of its own. See `user_account_service.py`
on why the translation is not skippable.
"""

from uuid import UUID

from app.modules.users.application.mappers import to_user_read
from app.modules.users.application.services.user_service import UserService
from app.modules.users.public.dtos import UserRead


class EmailVerificationWriter:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def mark_email_verified(self, user_id: UUID) -> UserRead:
        return to_user_read(await self._users.mark_email_verified(user_id))
