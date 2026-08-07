"""The preference policy and the service around it — A64-021.3 §32.

Four tests over the two things that can silently go wrong here, and neither
of them is "does the toggle store a boolean":

  **A default is a promise about consent.** Nobody set these values; the
  code did, for every account that has never opened the screen. So the
  tests assert the *whole* resolved matrix rather than one cell, and they
  assert what an unimplemented channel reports — an `enabled: true` on a
  channel that does not deliver is a switch that begins delivering to
  everyone the day it ships.

  **A refusal must leave nothing behind.** §9's "one illegal change rejects
  the whole request; no partial writes" is a claim about the *repository*,
  not about the return value, so the fake records every call and the test
  asserts it was never touched.

The domain tests need no fakes at all, which is the point of keeping the
policy framework-free: they are pure functions over two enums.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.core.error_codes import ErrorCode
from app.modules.notifications.application.ports import DeliveryRequest
from app.modules.notifications.application.services import (
    DuplicatePreferenceChange,
    NotificationPreferenceService,
    PreferenceChange,
    PreferenceDeliveryPolicy,
)
from app.modules.notifications.domain.preference import (
    IN_APP_ONLY,
    DeliveryChannel,
    LockedReason,
    PreferenceRefused,
    effective,
    ensure_settable,
)
from app.modules.notifications.domain.record import NotificationCategory

_NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


class _NullUnitOfWork:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakePreferences:
    """A `NotificationPreferenceRepository` that remembers what it was asked.

    `writes` is the assertion surface for §9: a refused batch must leave it
    empty. `queries` counts `permitted` calls, which is how §11's "one query
    per batch, never per recipient" is stated as a test rather than as a
    comment.
    """

    def __init__(
        self,
        overrides: Mapping[tuple[NotificationCategory, DeliveryChannel], bool] | None = None,
        *,
        per_user: Mapping[tuple[UUID, NotificationCategory], bool] | None = None,
    ) -> None:
        self._overrides = dict(overrides or {})
        self._per_user = dict(per_user or {})
        self.writes: list[tuple[NotificationCategory, DeliveryChannel, bool]] = []
        self.queries = 0

    async def overrides_for(
        self, user_id: UUID
    ) -> Mapping[tuple[NotificationCategory, DeliveryChannel], bool]:
        return dict(self._overrides)

    async def replace(
        self,
        user_id: UUID,
        *,
        changes: Sequence[tuple[NotificationCategory, DeliveryChannel, bool]],
        at: datetime,
    ) -> None:
        self.writes.extend(changes)
        for category, channel, enabled in changes:
            self._overrides[(category, channel)] = enabled

    async def permitted(
        self, requests: Sequence[DeliveryRequest], *, channel: DeliveryChannel
    ) -> frozenset[DeliveryRequest]:
        self.queries += 1
        return frozenset(
            request
            for request in requests
            if self._per_user.get(
                (request.recipient_id, request.category),
                channel is DeliveryChannel.IN_APP,
            )
        )


class TestTheResolvedMatrix:
    def test_a_new_account_gets_the_whole_matrix_with_both_locks_explained(self) -> None:
        """What every player who has never opened the screen receives.

        Asserted as the **whole** grid rather than a cell, because these
        values are consent nobody gave: the code chose them, for everyone.
        Three facts have to hold together, and a per-cell test would let any
        two of them pass while the third silently changed —

            in-app on, email and push off
            `system`/`in_app` on and not editable
            every uneditable cell says *which* kind of uneditable it is

        The two locks are deliberately different sentences: one means "we
        must be able to reach you", the other means "this does not work
        yet", and rendering "not allowed" for a channel that is merely
        unbuilt is the wrong thing to tell a player.
        """
        resolved = {
            (setting.category, setting.channel): (
                setting.enabled,
                setting.editable,
                setting.locked_reason,
            )
            for setting in effective({}, IN_APP_ONLY)
        }

        expected: dict[
            tuple[NotificationCategory, DeliveryChannel],
            tuple[bool, bool, LockedReason | None],
        ] = {
            (category, channel): (False, False, LockedReason.CHANNEL_UNAVAILABLE)
            for category in NotificationCategory
            for channel in (DeliveryChannel.EMAIL, DeliveryChannel.PUSH)
        }
        for category in NotificationCategory:
            locked = category is NotificationCategory.SYSTEM
            expected[(category, DeliveryChannel.IN_APP)] = (
                True,
                not locked,
                LockedReason.ESSENTIAL if locked else None,
            )

        assert resolved == expected


class TestWhatCannotBeChanged:
    def test_both_kinds_of_locked_change_are_refused_with_their_own_code(self) -> None:
        """§5: the backend enforces this, not the form. A hand-written
        request reaches the same function a click does — and it is told
        *which* refusal it hit, because "you may not" and "not built yet"
        are different sentences to render (§20)."""
        with pytest.raises(PreferenceRefused) as muting_essential:
            ensure_settable(NotificationCategory.SYSTEM, DeliveryChannel.IN_APP, False, IN_APP_ONLY)
        with pytest.raises(PreferenceRefused) as enabling_dead_channel:
            ensure_settable(NotificationCategory.SOCIAL, DeliveryChannel.PUSH, True, IN_APP_ONLY)

        assert (muting_essential.value.code, enabling_dead_channel.value.code) == (
            ErrorCode.NOTIFICATION_PREFERENCE_LOCKED,
            ErrorCode.NOTIFICATION_CHANNEL_UNAVAILABLE,
        )


class TestApplyingChanges:
    @pytest.mark.asyncio
    async def test_a_rejected_batch_writes_nothing_at_all(self) -> None:
        """§9: "one illegal change rejects the whole request; no partial
        writes." That is a claim about the **repository**, not about the
        return value, so what is asserted is that it was never touched.

        Both ways a batch can be rejected are exercised against the same
        fake, because the guarantee is the same one: a batch whose legal
        half was listed *first* — so a service that validated as it wrote
        would already have committed it — and a batch that names one switch
        twice, which has no intent to commit.
        """
        preferences = _FakePreferences()
        service = NotificationPreferenceService(
            preferences=preferences,
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
            availability=IN_APP_ONLY,
        )
        legal = PreferenceChange(
            category=NotificationCategory.SOCIAL,
            channel=DeliveryChannel.IN_APP,
            enabled=False,
        )
        locked = PreferenceChange(
            category=NotificationCategory.SYSTEM,
            channel=DeliveryChannel.IN_APP,
            enabled=False,
        )

        with pytest.raises(PreferenceRefused):
            await service.apply(uuid4(), changes=[legal, locked])
        with pytest.raises(DuplicatePreferenceChange):
            await service.apply(uuid4(), changes=[legal, legal])

        assert preferences.writes == []


class TestTheDeliveryPolicy:
    @pytest.mark.asyncio
    async def test_a_batch_costs_one_query_and_drops_only_the_muted(self) -> None:
        muted, listening = uuid4(), uuid4()
        preferences = _FakePreferences(per_user={(muted, NotificationCategory.SOCIAL): False})
        policy = PreferenceDeliveryPolicy(preferences=preferences)
        requests = [
            DeliveryRequest(recipient_id=muted, category=NotificationCategory.SOCIAL),
            DeliveryRequest(recipient_id=listening, category=NotificationCategory.SOCIAL),
        ]

        allowed = await policy.permitted(requests, channel=DeliveryChannel.IN_APP)

        # One call for two recipients, and only the muted one dropped — §11's
        # "a fan-out must not become one query per entrant", stated as an
        # assertion rather than as a comment.
        assert (allowed, preferences.queries) == (frozenset({requests[1]}), 1)
