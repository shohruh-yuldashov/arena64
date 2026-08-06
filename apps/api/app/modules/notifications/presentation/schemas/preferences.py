"""Wire schemas for `/notifications/preferences` — A64-021.3 §7, §14.

## The response is the whole matrix, always

Every category on every channel, with its default already resolved. A
response that carried only the rows a player had overridden would make the
client reimplement `default_enabled`, and the two would disagree the first
time a default changed — the guessing §7 exists to prevent.

## Four flags per cell, because they are four different facts

    enabled        what delivery does right now
    available      whether this build can deliver on this channel at all
    editable       whether this player may change it
    locked_reason  why not, when they may not

A client with only `enabled` would have to infer the rest from a hardcoded
list of which categories are essential — a second copy of the policy, in
another language, that nobody updates when the first one changes.

## The patch is a list of changes, not a matrix

`{"changes": [...]}` rather than the whole grid, for two reasons that both
matter on a settings screen. A client sending the full matrix would
overwrite a switch it never showed the day a category is added, and a save
that names only what moved is one a second tab cannot silently revert.
"""

from collections.abc import Sequence
from typing import Final

from pydantic import Field

from app.core.dto import BaseRequestDTO, BaseResponseDTO
from app.modules.notifications.application.services import PreferenceChange
from app.modules.notifications.domain.preference import DeliveryChannel, PreferenceSetting
from app.modules.notifications.domain.record import NotificationCategory

#: How many `(category, channel)` pairs exist. Derived rather than written
#: down, so a new category cannot leave a request bound that silently
#: refuses a legitimate full-matrix save.
_MATRIX_SIZE: Final[int] = len(NotificationCategory) * len(DeliveryChannel)


class PreferenceSettingResponse(BaseResponseDTO):
    """One category on one channel, as a client renders it."""

    category: str = Field(description="A `NotificationCategory` value.", examples=["social"])
    channel: str = Field(description="A `DeliveryChannel` value.", examples=["in_app"])
    enabled: bool = Field(description="Whether delivery happens on this pair right now.")
    available: bool = Field(
        description=(
            "Whether this build delivers on this channel at all. A **backend** "
            "fact — separate from whether the browser supports it."
        )
    )
    editable: bool = Field(description="Whether this player may change this pair.")
    locked_reason: str | None = Field(
        description=(
            "Why it cannot be changed — a `LockedReason` value, or `null` when "
            "it can. Clients render a translated explanation from it."
        ),
        examples=["essential"],
    )

    @classmethod
    def of(cls, setting: PreferenceSetting) -> "PreferenceSettingResponse":
        return cls(
            category=setting.category.value,
            channel=setting.channel.value,
            enabled=setting.enabled,
            available=setting.available,
            editable=setting.editable,
            locked_reason=setting.locked_reason.value if setting.locked_reason else None,
        )


class NotificationPreferencesResponse(BaseResponseDTO):
    """Every preference this player has, defaults resolved.

    Returned by both the read and the update, so a save needs no follow-up
    request and the screen cannot disagree with the server (§17).
    """

    settings: Sequence[PreferenceSettingResponse]

    @classmethod
    def of(cls, settings: Sequence[PreferenceSetting]) -> "NotificationPreferencesResponse":
        return cls(settings=[PreferenceSettingResponse.of(setting) for setting in settings])


class PreferenceChangeRequest(BaseRequestDTO):
    """One switch a player moved.

    The enums are the wire types, so an unknown category is a `422` from
    FastAPI's own validation before any service runs — tier 1 in
    services.md §6, and the reason the domain never sees a string it does
    not know.
    """

    category: NotificationCategory
    channel: DeliveryChannel
    enabled: bool

    def to_change(self) -> PreferenceChange:
        return PreferenceChange(category=self.category, channel=self.channel, enabled=self.enabled)


class UpdateNotificationPreferencesRequest(BaseRequestDTO):
    """What a save sends: only the switches that moved.

    Bounded at the size of the whole matrix, so a request cannot
    name a pair more times than there are pairs (§11). The duplicate check
    that rejects the same pair twice is the service's; this only stops an
    unbounded body reaching it.

    An empty list is legal and is a no-op that returns the current state. A
    form submitted with nothing dirty is not an error.
    """

    changes: Sequence[PreferenceChangeRequest] = Field(max_length=_MATRIX_SIZE)


__all__ = [
    "NotificationPreferencesResponse",
    "PreferenceChangeRequest",
    "PreferenceSettingResponse",
    "UpdateNotificationPreferencesRequest",
]
