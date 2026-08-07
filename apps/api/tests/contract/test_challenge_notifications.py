"""Friend challenge events becoming notifications — A64-022.4 §29.

Against real PostgreSQL and through the **production composition**: the
dispatcher is built by the factory `app_factory` calls, over a real outbox
and a real relay, and the events are the real event objects serialised by
their own `payload()`.

## What each test is actually about

  **An invitation reaches the recipient, and only them.** The person who
  acted is never told what they just did, which is the rule every social
  notification on this platform follows.

  **A muted category produces no row at all** — suppression happens where
  the notification would have been created, so there is nothing to hide on
  read and nothing for the badge to count.

  **Push is owed per live device**, and only for a recipient who asked for
  it. A player with no subscription costs no row.

  **A block that arrived after the challenge suppresses delivery.** The
  event is minutes old by the time the relay reaches it, and that interval
  is exactly where a delayed push would leak somebody the recipient has
  since removed.

  **An acceptance carries the match**, which is the fact the whole type
  exists to deliver: the challenger learns there is a board waiting and can
  reach it in one tap, inside a ten-minute join window.

  **A redelivered event writes nothing twice** — one durable row, one push
  debt, on the identity `(recipient_id, source_event_id, type)` the platform
  already had.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import OutboxSettings
from app.core.clock import SystemClock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.domain.block import Block
from app.modules.friends.infrastructure.cache import NoSocialGraphCache
from app.modules.friends.infrastructure.repositories import SqlAlchemyBlockedPlayerRepository
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.public import FriendChallengeAccepted, FriendChallengeCreated
from app.modules.notifications.application.services.challenge_notification_dispatcher import (
    CONSUMER_NAME as CHALLENGE_CONSUMER,
)
from app.modules.notifications.domain.preference import (
    IN_APP_ONLY,
    ChannelAvailability,
    DeliveryChannel,
)
from app.modules.notifications.domain.push import PUSH_CAPABLE_TYPES
from app.modules.notifications.domain.record import (
    ChallengeSummary,
    NavigationTargetType,
    NotificationCategory,
    NotificationType,
)
from app.modules.notifications.domain.subscription import PushSubscription
from app.modules.notifications.infrastructure.models import NotificationPushDeliveryModel
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyNotificationPreferenceRepository,
    SqlAlchemyNotificationRepository,
    SqlAlchemyPushSubscriptionRepository,
)
from app.modules.notifications.presentation.dependencies import (
    build_challenge_notification_dispatcher,
    build_durable_notification_writer,
)
from app.modules.reference.public import TimeControlId
from app.platform.outbox import (
    OutboxEventPublisher,
    OutboxRelay,
    SqlAlchemyOutboxRepository,
    SqlAlchemyProcessedEventStore,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

#: A challenge's own window — A64-022.1. Carried on the created event, so a
#: consumer delivering late can tell whether it is still answerable.
EXPIRES_AT = NOW + timedelta(hours=24)

#: What this suite's process delivers on.
#:
#: Both channels, because every claim here is about the pair: a durable row
#: **and** the push debt written in its transaction. `IN_APP_ONLY` would make
#: the push assertions vacuously true, and a push-only availability would
#: suppress the durable row the tests read back.
_IN_APP_AND_PUSH = ChannelAvailability.of(DeliveryChannel.IN_APP, DeliveryChannel.PUSH)


class _NoProfiles:
    """A `ProfileRenderer` that knows nobody.

    Every test here is about *which* notification is written and to whom,
    never about the actor snapshot — and an opponent with no public profile
    is a real production case (a deactivated account) that exercises the
    same branch without registering four users.
    """

    async def render_many(
        self, player_ids: Sequence[UUID], *, relationship: Any
    ) -> dict[UUID, Any]:
        return {}


def _dispatcher(session: AsyncSession, *, availability: ChannelAvailability | None = None) -> Any:
    """The consumer, assembled exactly as `app_factory` assembles it."""
    return build_challenge_notification_dispatcher(
        session,
        cache=NoSocialGraphCache(),
        profiles=_NoProfiles(),  # type: ignore[arg-type]
        store=build_durable_notification_writer(
            session,
            availability=availability or _IN_APP_AND_PUSH,
        ),
    )


async def _publish(session: AsyncSession, event: Any) -> None:
    """One real event, through the real outbox.

    The **real event object**, not a hand-written payload dict: the event is
    the contract between `matchmaking` and this consumer, so it is built by
    its own constructor and serialised by its own `payload()`. A dict here
    would let the two drift and the suite would not notice.
    """
    await OutboxEventPublisher(SqlAlchemyOutboxRepository(session)).publish(event)
    await session.flush()


def _created(
    *, challenge_id: UUID, challenger_id: UUID, recipient_id: UUID
) -> FriendChallengeCreated:
    return FriendChallengeCreated(
        occurred_at=NOW,
        challenge_id=challenge_id,
        challenger_id=challenger_id,
        recipient_id=recipient_id,
        time_control_id=TimeControlId.BLITZ_3_2,
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        expires_at=EXPIRES_AT,
    )


def _accepted(
    *, challenge_id: UUID, challenger_id: UUID, recipient_id: UUID, match_id: UUID
) -> FriendChallengeAccepted:
    return FriendChallengeAccepted(
        occurred_at=NOW,
        challenge_id=challenge_id,
        challenger_id=challenger_id,
        recipient_id=recipient_id,
        match_id=match_id,
        time_control_id=TimeControlId.BLITZ_3_2,
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
    )


async def _drain(session: AsyncSession, dispatcher: Any) -> Any:
    """One relay tick over the real outbox.

    Driven explicitly rather than by the worker's timer: a suite that slept
    would depend on wall-clock time, which CLAUDE.md §6.4 rules out.
    """
    settings = OutboxSettings()
    relay = OutboxRelay(
        outbox=SqlAlchemyOutboxRepository(session),
        processed=SqlAlchemyProcessedEventStore(session),
        handlers=[dispatcher],
        unit_of_work=SessionUnitOfWork(session),
        clock=SystemClock(),
        worker_id=f"contract-{CHALLENGE_CONSUMER}",
        batch_size=settings.batch_size,
        max_attempts=settings.max_attempts,
        retry_base_seconds=settings.retry_base_seconds,
        retry_max_seconds=settings.retry_max_seconds,
    )
    return await relay.run_once()


async def _notifications(session: AsyncSession, recipient: UUID) -> list[Any]:
    page = await SqlAlchemyNotificationRepository(session).list_for(
        recipient, after=None, limit=200
    )
    return list(page.entries)


async def _push_debt(session: AsyncSession, recipient: UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(NotificationPushDeliveryModel)
            .where(NotificationPushDeliveryModel.recipient_id == recipient)
        )
    ) or 0


async def _subscribe(session: AsyncSession, user_id: UUID) -> None:
    """One live browser for this player, so a push can be owed at all."""
    await SqlAlchemyPushSubscriptionRepository(session).register(
        PushSubscription(
            id=uuid4(),
            user_id=user_id,
            endpoint=f"https://push.example.com/wpush/{uuid4().hex}",
            # The keys are never used here: no message is encrypted in this
            # suite, which is `test_push_notifications.py`'s subject. What
            # matters is that a **live** row exists, because that is what
            # decides whether a push is owed at all.
            p256dh=b"\x04" + b"\x01" * 64,
            auth=b"\x02" * 16,
            created_at=NOW,
            updated_at=NOW,
            last_seen_at=NOW,
        )
    )


class TestInvitation:
    async def test_the_recipient_is_told_and_the_challenger_is_not(
        self, contract_session: AsyncSession
    ) -> None:
        """§29.1, and the mapping the whole phase turns on.

        The person told is the one who did **not** act. Asserting the
        challenger's empty inbox is half the test: a consumer that told both
        would pass an assertion about the recipient alone.
        """
        challenger, recipient, challenge_id = uuid4(), uuid4(), uuid4()
        await _publish(
            contract_session,
            _created(challenge_id=challenge_id, challenger_id=challenger, recipient_id=recipient),
        )

        await _drain(contract_session, _dispatcher(contract_session))

        stored = await _notifications(contract_session, recipient)
        assert [record.type for record in stored] == [NotificationType.FRIEND_CHALLENGE_RECEIVED]
        assert await _notifications(contract_session, challenger) == []

        record = stored[0]
        assert record.category is NotificationCategory.SOCIAL
        payload = record.payload
        assert isinstance(payload, ChallengeSummary)
        # The settings travel with the invitation, so the recipient learns
        # what they are being asked to play without opening anything.
        assert (payload.challenge_id, payload.rated) == (challenge_id, True)
        assert payload.time_control_id == TimeControlId.BLITZ_3_2.value
        assert payload.expires_at == EXPIRES_AT
        # No match exists until somebody says yes.
        assert payload.match_id is None
        # A placeholder with a date on it — A64-022.5 replaces this target.
        assert record.target.type is NavigationTargetType.FRIENDS
        assert record.target.ref is None

    async def test_a_muted_social_category_produces_no_row(
        self, contract_session: AsyncSession
    ) -> None:
        """§29.2. Suppression happens where the row would have been created,
        so there is nothing to hide on read and nothing to count."""
        challenger, muted = uuid4(), uuid4()
        await SqlAlchemyNotificationPreferenceRepository(
            contract_session, availability=IN_APP_ONLY
        ).replace(
            muted,
            changes=[(NotificationCategory.SOCIAL, DeliveryChannel.IN_APP, False)],
            at=NOW,
        )

        await _publish(
            contract_session,
            _created(challenge_id=uuid4(), challenger_id=challenger, recipient_id=muted),
        )
        await _drain(contract_session, _dispatcher(contract_session))

        assert await _notifications(contract_session, muted) == []
        assert await _push_debt(contract_session, muted) == 0

    async def test_a_live_browser_is_owed_one_push(self, contract_session: AsyncSession) -> None:
        """§29.3. A push is owed per **device**, in the notification's own
        transaction — and only because this type is pushable at all."""
        challenger, recipient = uuid4(), uuid4()
        await _subscribe(contract_session, recipient)

        await _publish(
            contract_session,
            _created(challenge_id=uuid4(), challenger_id=challenger, recipient_id=recipient),
        )
        await _drain(contract_session, _dispatcher(contract_session))

        assert await _push_debt(contract_session, recipient) == 1
        assert NotificationType.FRIEND_CHALLENGE_RECEIVED in PUSH_CAPABLE_TYPES

    async def test_a_block_after_the_challenge_suppresses_delivery(
        self, contract_session: AsyncSession
    ) -> None:
        """§29.4, and the reason the block list is re-read at delivery.

        The block is created **after** the event is published, which is the
        production case: the relay reaches an entry seconds or minutes
        later, and a delayed push is exactly the path a block must close.
        """
        challenger, recipient = uuid4(), uuid4()
        await _subscribe(contract_session, recipient)
        await _publish(
            contract_session,
            _created(challenge_id=uuid4(), challenger_id=challenger, recipient_id=recipient),
        )

        await SqlAlchemyBlockedPlayerRepository(contract_session).add(
            Block.place(blocker_id=recipient, blocked_id=challenger, at=NOW)
        )
        await contract_session.flush()

        await _drain(contract_session, _dispatcher(contract_session))

        assert await _notifications(contract_session, recipient) == []
        # No row means no push debt: the two are written in one transaction,
        # so there is no arrangement where a suppressed notification leaves
        # a delivery behind.
        assert await _push_debt(contract_session, recipient) == 0


class TestAcceptance:
    async def test_the_challenger_is_told_and_can_reach_the_match(
        self, contract_session: AsyncSession
    ) -> None:
        """§29.5, and the handoff this type exists for.

        The recipient already holds the match id — it is in their accept
        response. This is how the **challenger** learns there is a board
        waiting, and the target is the board rather than a list because the
        join window is ten minutes.
        """
        challenger, recipient = uuid4(), uuid4()
        match_id, challenge_id = uuid4(), uuid4()
        await _publish(
            contract_session,
            _accepted(
                challenge_id=challenge_id,
                challenger_id=challenger,
                recipient_id=recipient,
                match_id=match_id,
            ),
        )

        await _drain(contract_session, _dispatcher(contract_session))

        stored = await _notifications(contract_session, challenger)
        assert [record.type for record in stored] == [NotificationType.FRIEND_CHALLENGE_ACCEPTED]
        # The person who accepted is not told what they just did.
        assert await _notifications(contract_session, recipient) == []

        payload = stored[0].payload
        assert isinstance(payload, ChallengeSummary)
        assert (payload.challenge_id, payload.match_id) == (challenge_id, match_id)
        # An answered challenge has no remaining window.
        assert payload.expires_at is None
        # An id, never a URL: the client owns `/games/{id}`, and the push
        # worker substitutes the same ref into the same shape.
        assert stored[0].target.type is NavigationTargetType.LIVE_GAME
        assert stored[0].target.ref == str(match_id)

    async def test_a_block_does_not_withhold_an_accepted_match(
        self, contract_session: AsyncSession
    ) -> None:
        """The deliberate asymmetry — §18.

        Acceptance revalidated the relationship in its own transaction and
        created a match that exists. Withholding this would leave a
        challenger holding a game nobody told them about, so a block
        arriving afterwards suppresses nothing.
        """
        challenger, recipient = uuid4(), uuid4()
        await _publish(
            contract_session,
            _accepted(
                challenge_id=uuid4(),
                challenger_id=challenger,
                recipient_id=recipient,
                match_id=uuid4(),
            ),
        )
        await SqlAlchemyBlockedPlayerRepository(contract_session).add(
            Block.place(blocker_id=challenger, blocked_id=recipient, at=NOW)
        )
        await contract_session.flush()

        await _drain(contract_session, _dispatcher(contract_session))

        assert len(await _notifications(contract_session, challenger)) == 1


class TestExactlyOnce:
    async def test_a_redelivered_event_writes_nothing_twice(
        self, contract_session: AsyncSession
    ) -> None:
        """§29.7. One row and one push debt, on the identity the platform
        already had — `(recipient_id, source_event_id, type)`.

        The ledger is bypassed deliberately: a second drain would find the
        entry already processed and prove nothing about the write path. What
        is exercised here is the constraint underneath it, which is what
        actually holds when a consumer delivered and then died before the
        ledger committed.
        """
        challenger, recipient = uuid4(), uuid4()
        await _subscribe(contract_session, recipient)
        await _publish(
            contract_session,
            _created(challenge_id=uuid4(), challenger_id=challenger, recipient_id=recipient),
        )

        entries = await SqlAlchemyOutboxRepository(contract_session).claim(
            limit=10,
            claimed_by="contract-redelivery",
            now=SystemClock().now(),
            max_attempts=OutboxSettings().max_attempts,
        )
        dispatcher = _dispatcher(contract_session)
        await dispatcher.handle(entries)
        await dispatcher.handle(entries)

        assert len(await _notifications(contract_session, recipient)) == 1
        assert await _push_debt(contract_session, recipient) == 1


class TestLifecycleNoise:
    async def test_declined_cancelled_and_expired_are_not_subscribed(self) -> None:
        """§14. Three of the five lifecycle events notify nobody, and this
        consumer does not subscribe to them at all rather than subscribing
        and dropping them — so the ledger says a challenge lifecycle is five
        events of which two are notifications."""
        from app.modules.notifications.application.services.challenge_notification_dispatcher import (  # noqa: E501
            SUBSCRIBED_EVENT_TYPES,
        )

        assert (
            frozenset(
                {
                    "matchmaking.friend_challenge_created",
                    "matchmaking.friend_challenge_accepted",
                }
            )
            == SUBSCRIBED_EVENT_TYPES
        )
