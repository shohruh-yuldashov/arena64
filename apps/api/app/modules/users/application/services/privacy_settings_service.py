"""`PrivacySettingsService` — the implementation behind
`users.public.ports.PrivacySettingsEditor`.

The same shape as the other eight published-port adapters: a thin
translation between `UserService`'s domain types and the published DTO,
with no rule of its own. See `user_account_service.py` on why the
translation is not skippable.

What is specific to this one is that it is the *only* way to write a
privacy flag from outside `users`. There is no other published method that
touches these columns — `ProfileEditor` cannot, `AvatarStore` cannot,
`UserAccountCreator` cannot — so "what may change a player's visibility" is
answerable by looking at one file.

Its read is equally narrow: it returns the owner's own five flags and has
no way to ask about anybody else. The flags a *consumer* needs in order to
render somebody else's profile are a different, smaller shape
(`ProfileVisibility`) reached through a different port, and no caller holds
both.
"""

from uuid import UUID

from app.modules.users.application.commands import UpdatePrivacySettings
from app.modules.users.application.mappers import to_privacy_settings
from app.modules.users.application.services.user_service import UserService
from app.modules.users.public.dtos import PrivacySettingsView
from app.modules.users.public.edits import PrivacyEdits


class PrivacySettingsService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def get_privacy_settings(self, user_id: UUID) -> PrivacySettingsView:
        return to_privacy_settings(await self._users.get_user(user_id))

    async def update_privacy_settings(
        self, user_id: UUID, edits: PrivacyEdits
    ) -> PrivacySettingsView:
        # Field by field rather than `UpdatePrivacySettings(**asdict(edits))`,
        # for the reason `mappers.py` gives about explicit mapping: a flag
        # added to `PrivacyEdits` must be wired here deliberately, and a
        # flag added to the internal command must never become externally
        # settable just because it exists. The `UNSET` sentinels pass
        # straight through — they *are* the partial-update signal.
        command = UpdatePrivacySettings(
            show_country=edits.show_country,
            show_last_seen=edits.show_last_seen,
            show_statistics=edits.show_statistics,
            show_online_status=edits.show_online_status,
            show_activity=edits.show_activity,
        )
        return to_privacy_settings(await self._users.update_privacy(user_id, command))
