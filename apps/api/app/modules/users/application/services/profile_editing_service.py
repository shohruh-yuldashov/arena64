"""`ProfileEditingService` — the implementation behind
`users.public.ports.ProfileEditor`.

The same shape as the other seven published-port adapters: a thin
translation between `UserService`'s domain types and the published DTO,
with no rule of its own. See `user_account_service.py` on why the
translation is not skippable.

What is specific to this one is the *narrowing* it performs. It accepts
`ProfileEdits` — five fields — and hands `UserService` an
`UpdateUserProfile` built from exactly those five. `UpdateUserProfile` is
this module's internal command and could grow a sixth field tomorrow
without any consumer noticing; the published shape could not. Translating
between them here rather than publishing the internal command is what
keeps "which fields are editable from outside `users`" a decision this
module makes, rather than one that leaks with whatever the command happens
to contain.
"""

from uuid import UUID

from app.modules.users.application.commands import UpdateUserProfile
from app.modules.users.application.mappers import to_own_profile
from app.modules.users.application.services.user_service import UserService
from app.modules.users.public.dtos import OwnUserProfile
from app.modules.users.public.edits import ProfileEdits


class ProfileEditingService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def get_own_profile(self, user_id: UUID) -> OwnUserProfile:
        return to_own_profile(await self._users.get_user(user_id))

    async def update_own_profile(self, user_id: UUID, edits: ProfileEdits) -> OwnUserProfile:
        # Field by field rather than `UpdateUserProfile(**asdict(edits))`,
        # for the reason `mappers.py` gives about explicit mapping: a field
        # added to `ProfileEdits` must be wired here deliberately, and a
        # field added to `UpdateUserProfile` must never become externally
        # settable just because it exists. The `UNSET` sentinels pass
        # straight through — they *are* the partial-update signal.
        command = UpdateUserProfile(
            display_name=edits.display_name,
            bio=edits.bio,
            country=edits.country,
            preferred_language=edits.preferred_language,
            timezone=edits.timezone,
        )
        return to_own_profile(await self._users.update_profile(user_id, command))
