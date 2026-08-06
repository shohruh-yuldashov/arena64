"""`NotificationPreferenceService` — reading and changing a player's own
notification settings. A64-021.3 §7, §8, §9.

Thin, like every read service on this platform: the policy is
`domain.preference`'s and the storage is the repository's, and what is left
here is the transaction boundary and one rule the domain cannot express on
its own — that a patch is **all or nothing**.

## Validated whole, then written whole

§9: "validate all changes before committing; one illegal change rejects the
whole request; no partial writes." So every change is checked against the
domain first and only then handed to the repository, inside one unit of
work. A caller that sent one legal and one locked change gets a refusal and
a table that never moved.

That ordering is not merely tidy. Validating as you write would leave the
legal half committed and the caller unable to tell which half — and a
settings form that half-saves is a form nobody can trust twice.

## What this service cannot do

There is no method that names another player. Every entry point takes the
id its caller resolved from `CurrentUser`, and the repository has no
unscoped read, so this could not serve somebody else's preferences if it
tried (§26).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.exceptions import ValidationError
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import NotificationPreferenceRepository
from app.modules.notifications.domain.preference import (
    DeliveryChannel,
    PreferenceRefused,
    PreferenceSetting,
    effective,
    ensure_settable,
)
from app.modules.notifications.domain.record import NotificationCategory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreferenceChange:
    """One requested change, already parsed into its enums.

    The presentation layer turns wire strings into these, so an unknown
    category never reaches the domain — it is a `422` at the boundary, which
    is where this platform rejects malformed input (§20).
    """

    category: NotificationCategory
    channel: DeliveryChannel
    enabled: bool


class DuplicatePreferenceChange(ValidationError):
    """One request named the same category and channel twice — `422`.

    Refused rather than resolved by last-write-wins, because there is no
    honest answer: a request containing both `enabled: true` and
    `enabled: false` for one switch does not have an intent, and picking one
    would make the outcome depend on list order.

    A `ValidationError` rather than a domain refusal, because it is a
    malformed *request* rather than a rejected *change* — no legal batch can
    produce it, and nothing on the settings screen can send it.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.DUPLICATE_PREFERENCE_CHANGE

    def __init__(self, *, category: NotificationCategory, channel: DeliveryChannel) -> None:
        super().__init__(f"{category.value}/{channel.value} appears more than once")
        self.category = category
        self.channel = channel


class NotificationPreferenceService:
    """One player's notification settings, read and changed."""

    def __init__(
        self,
        *,
        preferences: NotificationPreferenceRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._preferences = preferences
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def effective_for(self, user_id: UUID) -> tuple[PreferenceSetting, ...]:
        """The whole matrix, defaults resolved. One query."""
        return effective(await self._preferences.overrides_for(user_id))

    async def apply(
        self, user_id: UUID, *, changes: Sequence[PreferenceChange]
    ) -> tuple[PreferenceSetting, ...]:
        """Applies every change, or none, and returns the resulting matrix.

        Returning the **effective** state rather than an acknowledgement is
        what makes the client's cache authoritative without a second read
        (§17): the response is exactly what a fresh `GET` would say, so a
        save is one request and the screen cannot disagree with the server.

        An empty change list is a legal no-op that still returns the current
        state — a form submitted with nothing dirty should not be an error,
        and a client that reconciles from the response gets a free refresh.
        """
        _reject_duplicates(changes)
        for change in changes:
            # Raises `PreferenceRefused`. Every change is checked **before**
            # any is written; see this module's docstring.
            ensure_settable(change.category, change.channel, change.enabled)

        if changes:
            async with self._unit_of_work:
                await self._preferences.replace(
                    user_id,
                    changes=[(c.category, c.channel, c.enabled) for c in changes],
                    at=self._clock.now(),
                )
                await self._unit_of_work.commit()

            logger.info(
                "notification_preferences_updated",
                # The player and what they changed — never *to* what. A
                # settings value is a personal choice, and which categories
                # somebody mutes is the kind of detail a log aggregator has
                # broader read access to than the table it came from
                # (services.md §8.5).
                extra={
                    "user_id": str(user_id),
                    "changed": len(changes),
                    "categories": sorted({c.category.value for c in changes}),
                },
            )

        return await self.effective_for(user_id)


def _reject_duplicates(changes: Sequence[PreferenceChange]) -> None:
    seen: set[tuple[NotificationCategory, DeliveryChannel]] = set()
    for change in changes:
        key = (change.category, change.channel)
        if key in seen:
            raise DuplicatePreferenceChange(category=change.category, channel=change.channel)
        seen.add(key)


__all__ = [
    "DuplicatePreferenceChange",
    "NotificationPreferenceService",
    "PreferenceChange",
    "PreferenceRefused",
]
