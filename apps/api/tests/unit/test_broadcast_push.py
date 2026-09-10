"""An administrative announcement can interrupt, and only when asked — A64-031.D.

The defect this file exists for: the console had no way to send a push, and
the reason was not a missing button. `BroadcastExpander` wrote notification
rows directly, so it bypassed the one component that enqueues push deliveries
(`DurableNotificationWriter`), `BroadcastChannel` had a single member, and
`PLATFORM_ANNOUNCEMENT` was absent from `PUSH_CAPABLE_TYPES` — three places,
each of which alone was enough to guarantee silence.

## What these tests are actually about

  **The channel decides, per broadcast.** An `IN_APP` announcement enqueues
  nothing. This is the property that keeps the feature opt-in: the default is
  the quiet one, so a client that never learned about the field cannot start
  buzzing phones.

  **A preference is not overridden by an administrator.** §15. Push rows are
  written for recipients the policy already admitted, and the send-time
  worker asks again — so the two gates are independent and this asserts the
  first.

  **Only rows this batch inserted.** The expander's crash story is that a
  replayed batch writes nothing; if it enqueued pushes for rows it did not
  insert, a redelivery would push twice for one announcement.

  **A process that cannot push does not queue.** A deployment without a VAPID
  key must still deliver the in-app half rather than accumulating rows
  nothing will ever drain.

These are unit tests over fakes because every one of them is a decision the
expander makes before any I/O of consequence. That the rows *become* a push
is `PushDeliveryService`'s, and that the worker refuses a muted player at
send time is asserted in `test_notification_push.py`.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import (
    DeliveryRequest,
    DuePushDelivery,
)
from app.modules.notifications.application.services.broadcast_expander import BroadcastExpander
from app.modules.notifications.domain.broadcast import (
    Broadcast,
    BroadcastAudience,
    BroadcastChannel,
    BroadcastStatus,
)
from app.modules.notifications.domain.preference import (
    IN_APP_ONLY,
    ChannelAvailability,
    DeliveryChannel,
)
from app.modules.notifications.domain.record import NotificationRecord
from app.modules.notifications.domain.subscription import PushSubscription

NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
BROADCAST = UUID("019fe700-0000-7000-8000-000000000001")

ALICE = UUID("019fe700-0000-7000-8000-0000000000a1")
BOB = UUID("019fe700-0000-7000-8000-0000000000b2")

PUSH_AND_IN_APP: ChannelAvailability = ChannelAvailability.of(
    DeliveryChannel.IN_APP, DeliveryChannel.PUSH
)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _UnitOfWork:
    def __init__(self) -> None:
        self.committed = 0

    async def __aenter__(self) -> "_UnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        return None


class _Broadcasts:
    def __init__(self, broadcast: Broadcast | None) -> None:
        self._broadcast = broadcast
        self.finished: list[BroadcastStatus] = []
        self.progress: list[int] = []

    async def claim_next(self, *, now: datetime) -> Broadcast | None:
        claimed, self._broadcast = self._broadcast, None
        return claimed

    async def record_progress(
        self,
        broadcast_id: UUID,
        *,
        cursor: UUID | None,
        delivered: int,
        audience_size: int | None,
    ) -> None:
        self.progress.append(delivered)

    async def finish(
        self,
        broadcast_id: UUID,
        *,
        status: BroadcastStatus,
        at: datetime,
        failure_reason: str | None = None,
    ) -> None:
        self.finished.append(status)


class _Notifications:
    """Appends, and can be told which recipients are duplicates."""

    def __init__(self, *, duplicates: frozenset[UUID] = frozenset()) -> None:
        self.appended: list[NotificationRecord] = []
        self._duplicates = duplicates

    async def append(self, record: NotificationRecord) -> bool:
        if record.recipient_id in self._duplicates:
            return False
        self.appended.append(record)
        return True


class _Audience:
    def __init__(self, players: Sequence[UUID]) -> None:
        self._players = list(players)

    async def count_eligible(self) -> int:
        return len(self._players)

    async def page_eligible(self, *, after: UUID | None, limit: int) -> Sequence[UUID]:
        ordered = sorted(self._players)
        if after is not None:
            ordered = [player for player in ordered if player > after]
        return ordered[:limit]


class _Policy:
    """Admits everybody except those explicitly muted."""

    def __init__(self, *, muted: frozenset[UUID] = frozenset()) -> None:
        self._muted = muted
        self.channels_asked: list[DeliveryChannel] = []

    async def permitted(
        self, requests: Sequence[DeliveryRequest], *, channel: DeliveryChannel
    ) -> set[DeliveryRequest]:
        self.channels_asked.append(channel)
        return {r for r in requests if r.recipient_id not in self._muted}


class _Announcer:
    async def announce(self, announcements: Sequence[object]) -> None:
        return None


class _PushDeliveries:
    def __init__(self) -> None:
        self.enqueued: list[DuePushDelivery] = []
        self.calls = 0

    async def enqueue(self, deliveries: Sequence[DuePushDelivery], *, at: datetime) -> int:
        self.calls += 1
        self.enqueued.extend(deliveries)
        return len(deliveries)


class _Subscriptions:
    """Every player has one browser unless named in `without`."""

    def __init__(self, *, without: frozenset[UUID] = frozenset()) -> None:
        self._without = without
        self.queries = 0

    async def live_for_many(
        self, user_ids: Sequence[UUID]
    ) -> Mapping[UUID, list[PushSubscription]]:
        self.queries += 1
        return {
            user_id: [
                PushSubscription(
                    id=uuid4(),
                    user_id=user_id,
                    endpoint=f"https://push.example/{user_id}",
                    p256dh=b"p256dh-key",
                    auth=b"auth-secret",
                    created_at=NOW,
                    updated_at=NOW,
                    last_seen_at=NOW,
                )
            ]
            for user_id in user_ids
            if user_id not in self._without
        }


def _broadcast(
    *,
    channel: BroadcastChannel = BroadcastChannel.IN_APP_AND_PUSH,
    recipients: tuple[UUID, ...] = (ALICE, BOB),
) -> Broadcast:
    return Broadcast(
        id=BROADCAST,
        title="Scheduled maintenance",
        body="The platform is unavailable from 02:00.",
        locale="en",
        audience=BroadcastAudience.SPECIFIC_PLAYERS,
        channel=channel,
        status=BroadcastStatus.SENDING,
        created_by=uuid4(),
        created_at=NOW,
        idempotency_key="key-00000001",
        recipients=recipients,
    )


def _expander(
    broadcast: Broadcast,
    *,
    notifications: _Notifications | None = None,
    policy: _Policy | None = None,
    push_deliveries: _PushDeliveries | None = None,
    subscriptions: _Subscriptions | None = None,
    availability: ChannelAvailability = PUSH_AND_IN_APP,
) -> tuple[BroadcastExpander, _PushDeliveries, _Notifications]:
    pushes = push_deliveries or _PushDeliveries()
    notes = notifications or _Notifications()
    expander = BroadcastExpander(
        broadcasts=_Broadcasts(broadcast),  # type: ignore[arg-type]
        notifications=notes,  # type: ignore[arg-type]
        audience=_Audience(broadcast.recipients),
        policy=policy or _Policy(),  # type: ignore[arg-type]
        announcer=_Announcer(),
        push_deliveries=pushes,  # type: ignore[arg-type]
        subscriptions=subscriptions or _Subscriptions(),  # type: ignore[arg-type]
        availability=availability,
        clock=_Clock(),
        unit_of_work=cast(UnitOfWork, _UnitOfWork()),
    )
    return expander, pushes, notes


class TestTheChannelDecides:
    @pytest.mark.asyncio
    async def test_an_in_app_broadcast_enqueues_no_push(self) -> None:
        # The whole point of the field. An administrator who did not ask to
        # interrupt anybody does not interrupt anybody.
        expander, pushes, notes = _expander(_broadcast(channel=BroadcastChannel.IN_APP))

        written = await expander.run_once()

        assert written == 2
        assert len(notes.appended) == 2
        assert pushes.calls == 0
        assert pushes.enqueued == []

    @pytest.mark.asyncio
    async def test_a_push_broadcast_enqueues_one_delivery_per_browser(self) -> None:
        expander, pushes, _ = _expander(_broadcast())

        await expander.run_once()

        assert {delivery.recipient_id for delivery in pushes.enqueued} == {ALICE, BOB}
        assert all(
            delivery.notification_type.value == "platform_announcement"
            for delivery in pushes.enqueued
        )

    @pytest.mark.asyncio
    async def test_the_in_app_row_is_written_either_way(self) -> None:
        # A push is an addition, never a substitution — `BroadcastChannel`.
        # The announcement has to be readable afterwards, and the push
        # payload carries no text of its own.
        pushing, _, loud = _expander(_broadcast())
        quiet, _, silent = _expander(_broadcast(channel=BroadcastChannel.IN_APP))

        await pushing.run_once()
        await quiet.run_once()

        assert len(loud.appended) == len(silent.appended) == 2
        assert [record.recipient_id for record in loud.appended] == [
            record.recipient_id for record in silent.appended
        ]


class TestAnAdministratorDoesNotOverrideAPreference:
    @pytest.mark.asyncio
    async def test_a_muted_player_receives_neither_row_nor_push(self) -> None:
        # §15. The suppression happens once, before anything is written, so
        # there is no notification for a push to point at either.
        policy = _Policy(muted=frozenset({BOB}))
        expander, pushes, notes = _expander(_broadcast(), policy=policy)

        await expander.run_once()

        assert [record.recipient_id for record in notes.appended] == [ALICE]
        assert [delivery.recipient_id for delivery in pushes.enqueued] == [ALICE]

    @pytest.mark.asyncio
    async def test_the_send_time_gate_is_left_to_the_worker(self) -> None:
        # The expander asks the policy about IN_APP only. Push preference is
        # deliberately *not* consulted here — §14 requires it at delivery
        # time, so that muting push after a broadcast was queued still
        # works. The row must exist for that later refusal to be possible.
        policy = _Policy()
        expander, pushes, _ = _expander(_broadcast(), policy=policy)

        await expander.run_once()

        assert policy.channels_asked == [DeliveryChannel.IN_APP]
        assert len(pushes.enqueued) == 2


class TestOnlyWhatThisBatchWrote:
    @pytest.mark.asyncio
    async def test_a_replayed_batch_enqueues_nothing(self) -> None:
        # The crash story: rows written, cursor not recorded, batch repeated.
        # `append` reports `False` for every existing row, and a push for a
        # notification that already existed is a second buzz for one
        # announcement.
        notifications = _Notifications(duplicates=frozenset({ALICE, BOB}))
        expander, pushes, _ = _expander(_broadcast(), notifications=notifications)

        written = await expander.run_once()

        assert written == 0
        assert pushes.calls == 0

    @pytest.mark.asyncio
    async def test_a_partially_replayed_batch_pushes_only_the_new_row(self) -> None:
        notifications = _Notifications(duplicates=frozenset({ALICE}))
        expander, pushes, _ = _expander(_broadcast(), notifications=notifications)

        await expander.run_once()

        assert [delivery.recipient_id for delivery in pushes.enqueued] == [BOB]

    @pytest.mark.asyncio
    async def test_a_recipient_with_no_browser_produces_no_row(self) -> None:
        subscriptions = _Subscriptions(without=frozenset({BOB}))
        expander, pushes, notes = _expander(_broadcast(), subscriptions=subscriptions)

        await expander.run_once()

        assert len(notes.appended) == 2
        assert [delivery.recipient_id for delivery in pushes.enqueued] == [ALICE]


class TestAProcessThatCannotPush:
    @pytest.mark.asyncio
    async def test_queues_nothing_and_still_delivers_the_announcement(self) -> None:
        # A relay worker without a VAPID key. Rows it cannot drain would
        # accumulate and every one would resolve to
        # `skipped_channel_unavailable` — see `app_factory`, which registers
        # the broadcast task unconditionally and the push task only with a
        # provider.
        expander, pushes, notes = _expander(_broadcast(), availability=IN_APP_ONLY)

        written = await expander.run_once()

        assert written == 2
        assert len(notes.appended) == 2
        assert pushes.calls == 0

    @pytest.mark.asyncio
    async def test_does_not_even_read_the_subscriptions(self) -> None:
        subscriptions = _Subscriptions()
        expander, _, _ = _expander(
            _broadcast(), subscriptions=subscriptions, availability=IN_APP_ONLY
        )

        await expander.run_once()

        assert subscriptions.queries == 0
