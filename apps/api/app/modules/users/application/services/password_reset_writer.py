"""`PasswordResetWriter` — the implementation behind
`users.public.ports.PasswordResetter`.

The same shape as the other four published-port adapters, and for the same
reason: a thin translation between `UserService` and the published
surface, with no rule of its own. See `user_account_service.py` on why the
translation is not skippable.

Thinner than the rest, because there is nothing to translate — no DTO
comes back. What it still earns its file for is the *narrowing*: the
object `auth` receives has one method and can do one thing, rather than
being a `UserService` that could also rename people. That is the whole
point of the port layer, and it is worth six lines.
"""

from uuid import UUID

from app.modules.users.application.services.user_service import UserService


class PasswordResetWriter:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def reset_password(self, user_id: UUID, *, new_hash: str) -> None:
        await self._users.set_password_hash(user_id, new_hash=new_hash)
