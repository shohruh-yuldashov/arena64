"""`PreferencesService` — the implementation behind
`users.public.ports.PreferencesEditor`.

The same shape as the other nine published-port adapters: a thin
translation between `UserService`'s domain types and the published DTO,
with no rule of its own. See `user_account_service.py` on why the
translation is not skippable.

What is specific to this one is that it is now the **only** published way
to change a language or a timezone. Both were reachable through
`ProfileEditor` until A64-012.5 and are not any more, which is what
"avoid duplicated writable fields" means in practice: the question "what
can change a player's timezone" has one answer, and it is this file.

The translation preserves the two-level partiality exactly — an `UNSET`
group stays `UNSET`, and an `UNSET` field inside a present group stays
`UNSET`. Flattening either level here would silently turn "change one
setting" into "replace the group", which is the failure a settings screen
cannot see and a player only notices later.
"""

from uuid import UUID

from app.core.sentinels import UNSET, UnsetType, is_set
from app.modules.users.application.commands import (
    UpdateGameplayPreferences,
    UpdateLocalePreferences,
    UpdatePreferences,
)
from app.modules.users.application.mappers import to_preferences
from app.modules.users.application.services.user_service import UserService
from app.modules.users.public.dtos import PreferencesView
from app.modules.users.public.edits import GameplayEdits, LocaleEdits, PreferenceEdits


class PreferencesService:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def get_preferences(self, user_id: UUID) -> PreferencesView:
        return to_preferences(await self._users.get_user(user_id))

    async def update_preferences(self, user_id: UUID, edits: PreferenceEdits) -> PreferencesView:
        # Group by group and field by field rather than
        # `UpdatePreferences(**asdict(edits))`, for the reason `mappers.py`
        # gives about explicit mapping: a setting added to the published
        # shape must be wired here deliberately, and a field added to the
        # internal command must never become externally settable just
        # because it exists.
        command = UpdatePreferences(
            gameplay=_gameplay(edits.gameplay),
            locale=_locale(edits.locale),
        )
        return to_preferences(await self._users.update_preferences(user_id, command))


def _gameplay(edits: GameplayEdits | UnsetType) -> UpdateGameplayPreferences | UnsetType:
    """Translates the gameplay group, preserving "group not mentioned".

    Returning `UNSET` for an absent group rather than an empty
    `UpdateGameplayPreferences` matters even though the two would behave
    identically today: the service branches on `is_set(command.gameplay)`,
    and an always-present group would make that branch dead code and the
    distinction unavailable to whatever needs it next — an audit line, a
    per-group rate limit.
    """
    if is_set(edits):
        return UpdateGameplayPreferences(
            board_theme=edits.board_theme,
            piece_set=edits.piece_set,
            confirm_move=edits.confirm_move,
            show_coordinates=edits.show_coordinates,
            animation_speed=edits.animation_speed,
        )
    return UNSET


def _locale(edits: LocaleEdits | UnsetType) -> UpdateLocalePreferences | UnsetType:
    """Translates the locale group, preserving "group not mentioned"."""
    if is_set(edits):
        return UpdateLocalePreferences(
            preferred_language=edits.preferred_language,
            timezone=edits.timezone,
        )
    return UNSET
