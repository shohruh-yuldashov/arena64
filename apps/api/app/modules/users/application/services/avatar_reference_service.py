"""`AvatarReferenceService` — the implementation behind
`users.public.ports.AvatarStore`.

The same shape as the other six published-port adapters: a thin
translation between `UserService`'s domain types and the published DTO,
with no rule of its own. See `user_account_service.py` on why the
translation is not skippable.

What is specific to this one is that all three methods are about a
*reference*, and none of them can touch an image. That is not a
restriction this class imposes — it is one it cannot escape, because
nothing in `users` knows what an image is. `avatars` owns the bytes.
"""

from uuid import UUID

from app.modules.users.application.mappers import to_avatar_reference
from app.modules.users.application.services.user_service import UserService
from app.modules.users.public.dtos import AvatarReference


class AvatarReferenceService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def get_avatar(self, user_id: UUID) -> AvatarReference:
        return to_avatar_reference(await self._users.get_user(user_id))

    async def set_avatar(self, user_id: UUID, *, object_key: str) -> AvatarReference:
        return to_avatar_reference(await self._users.set_avatar(user_id, object_key=object_key))

    async def clear_avatar(self, user_id: UUID) -> AvatarReference:
        return to_avatar_reference(await self._users.clear_avatar(user_id))
