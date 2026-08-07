"""Notification email, end to end — A64-021.5 §31.

Against real PostgreSQL and through the composition root's own factories,
with a **fake provider implementing the production port** — §31 forbids a
real provider in CI and the port is what makes the substitution honest: the
service cannot tell this apart from a vendor.

## What each test is about

  **A verified recipient with the preference on gets exactly one email**,
  and the row that proves it is `sent`. This is the whole channel: an
  enqueue in the notification's transaction, a claim, two batch reads, a
  render and a send.

  **Every skip is an outcome, never an exception** (§6). A muted preference,
  an unverified address and a missing account each produce a terminal row
  with a name an operator can read.

  **A transient failure retries and a permanent one does not.** The first
  leaves the row owed with a later `next_attempt_at`; the second is done.

  **A provider that is down does not touch the in-app notification** (§29).
  That is the property the whole design exists for, and it is asserted on
  the notification rather than on the delivery.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Locale
from app.core.identifiers import generate_uuid7
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.notifications.application.ports import DueEmailDelivery
from app.modules.notifications.application.services.email_delivery_service import (
    EmailDeliveryService,
    PermanentEmailFailure,
)
from app.modules.notifications.application.services.preference_delivery_policy import (
    PreferenceDeliveryPolicy,
)
from app.modules.notifications.domain.email_delivery import (
    EmailDeliveryOutcome,
    EmailDeliveryStatus,
)
from app.modules.notifications.domain.preference import (
    ChannelAvailability,
    DeliveryChannel,
)
from app.modules.notifications.domain.record import (
    NavigationTarget,
    NavigationTargetType,
    NotificationCategory,
    NotificationRecord,
    NotificationType,
    TournamentSummary,
)
from app.modules.notifications.infrastructure.models import NotificationEmailDeliveryModel
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyEmailDeliveryRepository,
    SqlAlchemyNotificationPreferenceRepository,
    SqlAlchemyNotificationRepository,
)
from app.modules.notifications.presentation.email import TemplateEmailRenderer
from app.modules.users.public import EmailRecipient
from app.platform.email import EmailMessage
from app.platform.metrics import NullMetrics
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
ORIGIN = "https://arena64.gg"

#: The channel on, which is what a deployment with a provider configured has.
EMAIL_ON = ChannelAvailability.of(DeliveryChannel.IN_APP, DeliveryChannel.EMAIL)


class FakeProvider:
    """`platform.email.EmailProvider`, recording rather than sending.

    Structurally satisfies the production port, which is the point: the
    service holds the same type a vendor adapter would, so nothing about the
    path under test is special-cased for a test.
    """

    def __init__(self, *, fails_with: Exception | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self._fails_with = fails_with

    async def send(self, message: EmailMessage) -> None:
        if self._fails_with is not None:
            raise self._fails_with
        self.sent.append(message)


class FakeRecipients:
    """`users.public.EmailRecipientDirectory`, dictated by a test.

    `users`' own suite exercises the eligibility query. What matters here is
    that this service **asks**, and does the right thing with an absence —
    which is what an unverified or deleted account produces.
    """

    def __init__(self, *recipients: EmailRecipient) -> None:
        self._by_id = {recipient.user_id: recipient for recipient in recipients}
        self.queries = 0

    async def recipients_for(self, user_ids: Sequence[UUID]) -> Mapping[UUID, EmailRecipient]:
        self.queries += 1
        return {user_id: self._by_id[user_id] for user_id in user_ids if user_id in self._by_id}


def _recipient(user_id: UUID, *, locale: Locale = Locale.EN) -> EmailRecipient:
    return EmailRecipient(
        user_id=user_id,
        email=f"{user_id.hex[:8]}@example.com",
        locale=locale,
        display_name="Player",
    )


def _notification(recipient_id: UUID, *, name: str = "Sunday Open") -> NotificationRecord:
    return NotificationRecord(
        id=generate_uuid7(),
        recipient_id=recipient_id,
        type=NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED,
        category=NotificationCategory.TOURNAMENT,
        payload=TournamentSummary(tournament_id=uuid4(), tournament_name=name),
        target=NavigationTarget(type=NavigationTargetType.TOURNAMENT, ref=str(uuid4())),
        source_event_id=uuid4(),
        created_at=NOW,
    )


def _service(
    session: AsyncSession,
    *,
    provider: FakeProvider,
    recipients: FakeRecipients,
    clock: MovableClock | None = None,
    availability: ChannelAvailability = EMAIL_ON,
    max_attempts: int = 3,
) -> EmailDeliveryService:
    """The service, with the composition root's own collaborators.

    Assembled here rather than through `build_email_delivery_service` for one
    reason: that factory constructs the real recipient directory and the real
    transport, and both are what a test must substitute. Every other
    collaborator — the repositories, the policy, the renderer, the unit of
    work — is the production class.
    """
    return EmailDeliveryService(
        deliveries=SqlAlchemyEmailDeliveryRepository(session),
        notifications=SqlAlchemyNotificationRepository(session),
        recipients=recipients,  # type: ignore[arg-type]
        policy=PreferenceDeliveryPolicy(
            preferences=SqlAlchemyNotificationPreferenceRepository(
                session, availability=availability
            )
        ),
        renderer=TemplateEmailRenderer(public_origin=ORIGIN),
        provider=provider,  # type: ignore[arg-type]
        metrics=NullMetrics(),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock or MovableClock(NOW),
        availability=availability,
        batch_size=20,
        max_attempts=max_attempts,
        retry_base_seconds=60,
        retry_max_seconds=3600,
    )


async def _opt_in(session: AsyncSession, player: UUID) -> None:
    """Turns tournament email on for one player.

    Explicit in every test that expects delivery, because notification email
    is **opt-in**: A64-021.3 defaults every non-in-app channel to off, and
    A64-021.5 deliberately did not flip it — a player who had been told email
    was unavailable must not start receiving it the day a provider is
    configured.
    """
    await SqlAlchemyNotificationPreferenceRepository(session, availability=EMAIL_ON).replace(
        player,
        changes=[(NotificationCategory.TOURNAMENT, DeliveryChannel.EMAIL, True)],
        at=NOW,
    )


async def _owed(session: AsyncSession, record: NotificationRecord) -> None:
    """Stores the notification and the email it is owed, as the writer does."""
    assert await SqlAlchemyNotificationRepository(session).append(record)
    await SqlAlchemyEmailDeliveryRepository(session).enqueue(
        [
            DueEmailDelivery(
                notification_id=record.id,
                recipient_id=record.recipient_id,
                notification_type=record.type,
                attempt_count=0,
            )
        ],
        at=NOW,
    )
    await session.flush()


async def _row(session: AsyncSession, notification_id: UUID) -> NotificationEmailDeliveryModel:
    row = await session.scalar(
        select(NotificationEmailDeliveryModel).where(
            NotificationEmailDeliveryModel.notification_id == notification_id
        )
    )
    assert row is not None
    return row


class TestDelivery:
    async def test_a_verified_recipient_receives_one_email(
        self, contract_session: AsyncSession
    ) -> None:
        """§31.1. The whole channel, once.

        The message is asserted on its **content** rather than its shape: a
        subject a recipient would recognise, both parts present, and the
        call-to-action pointing at the configured origin. §31 forbids
        asserting on HTML whitespace, so nothing here does.
        """
        player = uuid4()
        record = _notification(player, name="Sunday Open")
        await _opt_in(contract_session, player)
        await _owed(contract_session, record)

        provider = FakeProvider()
        recipients = FakeRecipients(_recipient(player))
        result = await _service(
            contract_session, provider=provider, recipients=recipients
        ).deliver_once()

        assert result.outcomes == {EmailDeliveryOutcome.DELIVERED: 1}
        assert len(provider.sent) == 1
        message = provider.sent[0]
        assert "Tournament registration confirmed" in message.subject
        # §17: both parts, always. An HTML-only transactional email is what
        # this assertion exists to prevent.
        assert "Sunday Open" in message.text_body
        assert message.html_body is not None
        assert ORIGIN in message.text_body

        row = await _row(contract_session, record.id)
        assert row.status == EmailDeliveryStatus.SENT.value
        assert row.next_attempt_at is None, "a delivered row must never be claimable again"

    async def test_a_second_pass_sends_nothing(self, contract_session: AsyncSession) -> None:
        """§31.4. The idempotency is the row, not a memory of having sent.

        A second pass finds nothing due, because the first left the row
        terminal. That is what makes a restarted worker safe: the queue is in
        the database, and so is the fact that it was drained.
        """
        player = uuid4()
        record = _notification(player)
        await _opt_in(contract_session, player)
        await _owed(contract_session, record)

        provider = FakeProvider()
        recipients = FakeRecipients(_recipient(player))
        service = _service(contract_session, provider=provider, recipients=recipients)

        await service.deliver_once()
        second = await service.deliver_once()

        assert (second.claimed, len(provider.sent)) == (0, 1)

    async def test_the_locale_chooses_the_template(self, contract_session: AsyncSession) -> None:
        """§31.8. The recipient's **stored** language, never inferred.

        Asserted through the delivered message rather than by calling the
        renderer, because the claim is that the service reads the account's
        locale and hands it over — a renderer test would prove the templates
        and nothing about the wiring.
        """
        player = uuid4()
        await _opt_in(contract_session, player)
        await _owed(contract_session, _notification(player))

        provider = FakeProvider()
        recipients = FakeRecipients(_recipient(player, locale=Locale.UZ))
        await _service(contract_session, provider=provider, recipients=recipients).deliver_once()

        assert "Turnirga ro'yxatdan o'tish tasdiqlandi" in provider.sent[0].subject

    async def test_user_controlled_text_is_escaped_in_the_html_and_not_in_the_text(
        self, contract_session: AsyncSession
    ) -> None:
        """§31.9. A tournament name reaches an HTML document.

        Both halves matter and they pull in opposite directions: the markup
        must not carry a live tag, and the plain-text part must not carry an
        entity — `Bob &amp; Sons` in a text client is a bug that only shows
        up in the half nobody reviews.
        """
        player = uuid4()
        await _opt_in(contract_session, player)
        await _owed(
            contract_session,
            _notification(player, name="<script>alert(1)</script> & Sons"),
        )

        provider = FakeProvider()
        await _service(
            contract_session,
            provider=provider,
            recipients=FakeRecipients(_recipient(player)),
        ).deliver_once()

        message = provider.sent[0]
        assert message.html_body is not None
        assert "<script>" not in message.html_body
        assert "&lt;script&gt;" in message.html_body
        assert "&amp;" not in message.text_body
        assert "& Sons" in message.text_body


class TestSkips:
    async def test_a_muted_preference_sends_nothing_and_says_so(
        self, contract_session: AsyncSession
    ) -> None:
        """§31.2, §7. The preference is read **at delivery time**.

        Set after the delivery was enqueued, which is the case that matters:
        a player who mutes tournament email between a round publication and
        the worker reaching it has muted it.
        """
        player = uuid4()
        record = _notification(player)
        await _owed(contract_session, record)
        await SqlAlchemyNotificationPreferenceRepository(
            contract_session, availability=EMAIL_ON
        ).replace(
            player,
            changes=[(NotificationCategory.TOURNAMENT, DeliveryChannel.EMAIL, False)],
            at=NOW,
        )

        provider = FakeProvider()
        result = await _service(
            contract_session,
            provider=provider,
            recipients=FakeRecipients(_recipient(player)),
        ).deliver_once()

        assert provider.sent == []
        assert result.outcomes == {EmailDeliveryOutcome.SKIPPED_PREFERENCE: 1}
        row = await _row(contract_session, record.id)
        assert (row.status, row.outcome) == (
            EmailDeliveryStatus.SKIPPED.value,
            EmailDeliveryOutcome.SKIPPED_PREFERENCE.value,
        )

    async def test_an_ineligible_recipient_is_a_named_outcome_not_an_error(
        self, contract_session: AsyncSession
    ) -> None:
        """§31.3, §6. An unverified address, an absent account and a deleted
        one are one answer — the directory returns nothing for all three, and
        distinguishing them would be an account-existence oracle.

        A **terminal** row, not a retry: none of the three is fixed by asking
        again in a minute.
        """
        player = uuid4()
        record = _notification(player)
        await _opt_in(contract_session, player)
        await _owed(contract_session, record)

        provider = FakeProvider()
        result = await _service(
            contract_session,
            provider=provider,
            recipients=FakeRecipients(),  # nobody is eligible
        ).deliver_once()

        assert provider.sent == []
        assert result.outcomes == {EmailDeliveryOutcome.SKIPPED_NO_EMAIL: 1}
        row = await _row(contract_session, record.id)
        assert (row.status, row.next_attempt_at) == (EmailDeliveryStatus.SKIPPED.value, None)


class TestFailures:
    async def test_a_transient_failure_is_scheduled_again(
        self, contract_session: AsyncSession
    ) -> None:
        """§31.5. The row stays owed, later.

        An unclassified exception, deliberately: an adapter that did not
        recognise a fault must produce a retry rather than a silent drop, and
        this asserts the default direction rather than a classified one.
        """
        player = uuid4()
        record = _notification(player)
        await _opt_in(contract_session, player)
        await _owed(contract_session, record)

        result = await _service(
            contract_session,
            provider=FakeProvider(fails_with=TimeoutError("provider timed out")),
            recipients=FakeRecipients(_recipient(player)),
        ).deliver_once()

        assert result.outcomes == {EmailDeliveryOutcome.RETRYABLE_FAILURE: 1}
        row = await _row(contract_session, record.id)
        assert row.status == EmailDeliveryStatus.PENDING.value
        assert row.attempt_count == 1
        assert row.next_attempt_at == NOW + timedelta(seconds=60)

    async def test_a_permanent_failure_is_not_retried(self, contract_session: AsyncSession) -> None:
        """§31.6. A rejected address is asking the same question again."""
        player = uuid4()
        record = _notification(player)
        await _opt_in(contract_session, player)
        await _owed(contract_session, record)

        result = await _service(
            contract_session,
            provider=FakeProvider(fails_with=PermanentEmailFailure("no such mailbox")),
            recipients=FakeRecipients(_recipient(player)),
        ).deliver_once()

        assert result.outcomes == {EmailDeliveryOutcome.PERMANENT_FAILURE: 1}
        row = await _row(contract_session, record.id)
        assert (row.status, row.next_attempt_at) == (EmailDeliveryStatus.FAILED.value, None)

    async def test_retries_stop_at_the_limit(self, contract_session: AsyncSession) -> None:
        """§31.6. Bounded, and the row says which bound it hit.

        `attempts_exhausted` rather than `permanent_failure`, because the two
        mean different things to an operator: one is a bad address, the other
        is a provider that was down for hours.
        """
        player = uuid4()
        record = _notification(player)
        await _opt_in(contract_session, player)
        await _owed(contract_session, record)

        clock = MovableClock(NOW)
        service = _service(
            contract_session,
            provider=FakeProvider(fails_with=TimeoutError("still down")),
            recipients=FakeRecipients(_recipient(player)),
            clock=clock,
            max_attempts=3,
        )

        # Far enough past each `next_attempt_at` that the row is due again.
        for attempt in range(4):
            clock.advance(3600 * (attempt + 1))
            await service.deliver_once()

        row = await _row(contract_session, record.id)
        assert (row.status, row.outcome) == (
            EmailDeliveryStatus.FAILED.value,
            EmailDeliveryOutcome.ATTEMPTS_EXHAUSTED.value,
        )

    async def test_a_dead_provider_leaves_the_in_app_notification_untouched(
        self, contract_session: AsyncSession
    ) -> None:
        """§31.7, §29 — the property the whole design exists for.

        Asserted on the **notification**, not on the delivery: a player whose
        email bounced still has the record, still has it unread, and still
        counts toward their badge.
        """
        player = uuid4()
        record = _notification(player)
        await _opt_in(contract_session, player)
        await _owed(contract_session, record)

        await _service(
            contract_session,
            provider=FakeProvider(fails_with=TimeoutError("provider is down")),
            recipients=FakeRecipients(_recipient(player)),
        ).deliver_once()

        notifications = SqlAlchemyNotificationRepository(contract_session)
        page = await notifications.list_for(player, after=None, limit=10)
        assert [entry.id for entry in page.entries] == [record.id]
        assert await notifications.count_unread(player) == 1


class TestBatching:
    async def test_a_fan_out_reads_recipients_once(self, contract_session: AsyncSession) -> None:
        """§31.10, §30. One directory read for the whole pass.

        Sixteen deliveries rather than 128, because what is being asserted is
        that the count does not follow the batch — one read for sixteen is
        the same claim as one read for a hundred, and costs a fraction of the
        setup.
        """
        players = [uuid4() for _ in range(16)]
        for player in players:
            await _opt_in(contract_session, player)
            await _owed(contract_session, _notification(player))

        recipients = FakeRecipients(*[_recipient(player) for player in players])
        provider = FakeProvider()
        result = await _service(
            contract_session, provider=provider, recipients=recipients
        ).deliver_once()

        assert (result.claimed, len(provider.sent)) == (16, 16)
        assert recipients.queries == 1, "a lookup per recipient is the N+1 this bounds"


class TestChannelOff:
    async def test_a_process_that_cannot_send_skips_rather_than_fails(
        self, contract_session: AsyncSession
    ) -> None:
        """A row enqueued by a node with the channel on, claimed by one with
        it off. Nothing about the delivery is wrong, so it is skipped — and
        named, so an operator seeing a queue drain to `skipped` can tell it
        from one that was muted."""
        player = uuid4()
        record = _notification(player)
        await _owed(contract_session, record)

        provider = FakeProvider()
        result = await _service(
            contract_session,
            provider=provider,
            recipients=FakeRecipients(_recipient(player)),
            availability=ChannelAvailability.of(DeliveryChannel.IN_APP),
        ).deliver_once()

        assert provider.sent == []
        assert result.outcomes == {EmailDeliveryOutcome.SKIPPED_CHANNEL_UNAVAILABLE: 1}
