"""`EmailVerificationService` — orchestration, no database.

Runs the real `OpaqueTokenService` (SHA-256 is microseconds, and the
properties under test are only real if the hashing is) against
`FakeVerificationTokenRepository`, which the contract suite in
`tests/contract/test_verification_token_repository.py` holds to the same
behaviour as the SQLAlchemy adapter.

The clock is movable, so "this link expired yesterday" is an assignment
rather than a `sleep`.
"""

import logging
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.config.settings import EmailSettings
from app.core.enums import Locale
from app.modules.auth.application.services import (
    EmailVerificationService,
    OpaqueTokenService,
)
from app.modules.auth.domain.exceptions import InvalidVerificationToken
from app.modules.users.public import AvatarReference, UserRead
from app.platform.email import EmailMessage
from tests.fakes.verification_token_repository import FakeVerificationTokenRepository

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
USER_ID = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")
EMAIL = "player.one@example.com"


class MovableClock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


class _NullUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0

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
        self.commits += 1

    async def rollback(self) -> None:
        return None


def account(*, is_verified: bool = False) -> UserRead:
    return UserRead(
        id=USER_ID,
        username="player_one",
        email=EMAIL,
        display_name=None,
        bio=None,
        country=None,
        avatar=AvatarReference(object_key=None, version=1, uploaded_at=None),
        preferred_language=Locale.EN,
        timezone="UTC",
        is_active=True,
        is_verified=is_verified,
        created_at=NOW,
        updated_at=None,
    )


class _FakeProfiles:
    """`UserProfileReader` over one in-memory account."""

    def __init__(self, known: UserRead | None) -> None:
        self.known = known

    async def get_profile(self, user_id: UUID) -> UserRead:
        if self.known is None or self.known.id != user_id:
            from app.modules.users.public import UserNotFound

            raise UserNotFound("No such user.")
        return self.known

    async def find_by_email(self, email: str) -> UserRead | None:
        if self.known is None or self.known.email != email:
            return None
        return self.known


class _RecordingVerifier:
    """`EmailVerifier`. Records the write rather than performing one, and
    reflects it back so the service's return value is realistic."""

    def __init__(self, profiles: _FakeProfiles) -> None:
        self._profiles = profiles
        self.verified: list[UUID] = []

    async def mark_email_verified(self, user_id: UUID) -> UserRead:
        self.verified.append(user_id)
        known = self._profiles.known
        assert known is not None
        self._profiles.known = account(is_verified=True)
        return self._profiles.known


class _RecordingProvider:
    """`EmailProvider`. Keeps every message so tests can read the link out
    of the body — which is the only place the raw token legitimately
    exists once it has left the service."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _BrokenProvider:
    async def send(self, message: EmailMessage) -> None:
        raise RuntimeError("the mail provider is on fire")


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def settings() -> EmailSettings:
    return EmailSettings()


@pytest.fixture
def repository() -> FakeVerificationTokenRepository:
    return FakeVerificationTokenRepository()


@pytest.fixture
def profiles() -> _FakeProfiles:
    return _FakeProfiles(account())


@pytest.fixture
def verifier(profiles: _FakeProfiles) -> _RecordingVerifier:
    return _RecordingVerifier(profiles)


@pytest.fixture
def provider() -> _RecordingProvider:
    return _RecordingProvider()


@pytest.fixture
def unit_of_work() -> _NullUnitOfWork:
    return _NullUnitOfWork()


@pytest.fixture
def service(
    repository: FakeVerificationTokenRepository,
    profiles: _FakeProfiles,
    verifier: _RecordingVerifier,
    provider: _RecordingProvider,
    unit_of_work: _NullUnitOfWork,
    clock: MovableClock,
    settings: EmailSettings,
) -> EmailVerificationService:
    return EmailVerificationService(
        tokens=repository,
        token_factory=OpaqueTokenService(settings.token_entropy_bytes),
        profiles=profiles,
        verifier=verifier,
        email=provider,
        unit_of_work=unit_of_work,
        clock=clock,
        settings=settings,
    )


class TestCreateVerificationToken:
    async def test_returns_a_token_and_its_raw_value(
        self, service: EmailVerificationService
    ) -> None:
        issued = await service.create_verification_token(USER_ID)

        assert issued.token.user_id == USER_ID
        assert issued.raw_token

    async def test_stores_only_the_hash(
        self, service: EmailVerificationService, settings: EmailSettings
    ) -> None:
        """§4.5: a database read "must not yield a working" credential."""
        issued = await service.create_verification_token(USER_ID)

        expected = OpaqueTokenService(settings.token_entropy_bytes).hash(issued.raw_token)
        assert issued.token.token_hash == expected
        assert issued.raw_token.encode() not in issued.token.token_hash

    async def test_the_raw_token_is_absent_from_the_repr(
        self, service: EmailVerificationService
    ) -> None:
        issued = await service.create_verification_token(USER_ID)

        assert issued.raw_token not in repr(issued)

    async def test_carries_256_bits(self, service: EmailVerificationService) -> None:
        """DB-24's premise. Below 256 bits, hashing with SHA-256 rather
        than Argon2id stops being sound."""
        issued = await service.create_verification_token(USER_ID)

        assert len(issued.raw_token) >= 43

    async def test_expires_in_the_configured_window(
        self, service: EmailVerificationService, settings: EmailSettings
    ) -> None:
        issued = await service.create_verification_token(USER_ID)

        assert issued.token.expires_at == NOW + timedelta(
            hours=settings.verification_token_ttl_hours
        )

    async def test_commits(
        self, service: EmailVerificationService, unit_of_work: _NullUnitOfWork
    ) -> None:
        await service.create_verification_token(USER_ID)

        assert unit_of_work.commits == 1

    async def test_two_issuances_produce_different_tokens(
        self, service: EmailVerificationService
    ) -> None:
        first = await service.create_verification_token(USER_ID)
        second = await service.create_verification_token(USER_ID)

        assert first.raw_token != second.raw_token


class TestOnlyOneTokenIsEverLive:
    """database.md §4.5: "at most one live token per account"."""

    async def test_issuing_invalidates_the_previous_token(
        self, service: EmailVerificationService
    ) -> None:
        first = await service.create_verification_token(USER_ID)

        await service.create_verification_token(USER_ID)

        with pytest.raises(InvalidVerificationToken):
            await service.verify_email(first.raw_token)

    async def test_exactly_one_remains_active(
        self,
        service: EmailVerificationService,
        repository: FakeVerificationTokenRepository,
    ) -> None:
        for _ in range(3):
            await service.create_verification_token(USER_ID)

        assert await repository.count_active_for_user(USER_ID, at=NOW) == 1

    async def test_the_newest_token_is_the_one_that_works(
        self, service: EmailVerificationService
    ) -> None:
        await service.create_verification_token(USER_ID)
        newest = await service.create_verification_token(USER_ID)

        assert (await service.verify_email(newest.raw_token)).is_verified is True

    async def test_invalidate_previous_tokens_kills_the_live_one(
        self, service: EmailVerificationService
    ) -> None:
        issued = await service.create_verification_token(USER_ID)

        assert await service.invalidate_previous_tokens(USER_ID) == 1

        with pytest.raises(InvalidVerificationToken):
            await service.verify_email(issued.raw_token)

    async def test_invalidating_nothing_is_not_an_error(
        self, service: EmailVerificationService
    ) -> None:
        assert await service.invalidate_previous_tokens(USER_ID) == 0


class TestVerifyEmail:
    async def test_marks_the_account_verified(
        self, service: EmailVerificationService, verifier: _RecordingVerifier
    ) -> None:
        issued = await service.create_verification_token(USER_ID)

        verified = await service.verify_email(issued.raw_token)

        assert verified.is_verified is True
        assert verifier.verified == [USER_ID]

    async def test_works_up_to_the_last_second(
        self,
        service: EmailVerificationService,
        clock: MovableClock,
        settings: EmailSettings,
    ) -> None:
        issued = await service.create_verification_token(USER_ID)
        clock.instant = (
            NOW + timedelta(hours=settings.verification_token_ttl_hours) - timedelta(seconds=1)
        )

        assert (await service.verify_email(issued.raw_token)).is_verified is True

    async def test_rejects_an_expired_token(
        self,
        service: EmailVerificationService,
        clock: MovableClock,
        settings: EmailSettings,
    ) -> None:
        issued = await service.create_verification_token(USER_ID)
        clock.instant = NOW + timedelta(hours=settings.verification_token_ttl_hours)

        with pytest.raises(InvalidVerificationToken):
            await service.verify_email(issued.raw_token)

    async def test_an_expired_token_does_not_verify_the_account(
        self,
        service: EmailVerificationService,
        verifier: _RecordingVerifier,
        clock: MovableClock,
    ) -> None:
        issued = await service.create_verification_token(USER_ID)
        clock.instant = NOW + timedelta(days=2)

        with pytest.raises(InvalidVerificationToken):
            await service.verify_email(issued.raw_token)

        assert verifier.verified == []

    async def test_rejects_an_unknown_token(self, service: EmailVerificationService) -> None:
        with pytest.raises(InvalidVerificationToken):
            await service.verify_email("a-token-nobody-ever-issued")

    async def test_rejects_an_empty_token(self, service: EmailVerificationService) -> None:
        with pytest.raises(InvalidVerificationToken):
            await service.verify_email("")

    async def test_every_failure_carries_the_same_message(
        self, service: EmailVerificationService, clock: MovableClock
    ) -> None:
        """Unknown, used and expired are indistinguishable to a caller —
        the action is the same in all three, and separating them says
        whether a token the caller holds was ever real."""
        used = await service.create_verification_token(USER_ID)
        await service.verify_email(used.raw_token)
        expired = await service.create_verification_token(USER_ID)
        clock.instant = NOW + timedelta(days=2)

        messages = set()
        codes = set()
        for candidate in (used.raw_token, expired.raw_token, "never-issued"):
            with pytest.raises(InvalidVerificationToken) as raised:
                await service.verify_email(candidate)
            messages.add(raised.value.message)
            codes.add(raised.value.code)

        assert len(messages) == 1
        assert len(codes) == 1

    async def test_the_message_never_contains_the_token(
        self, service: EmailVerificationService
    ) -> None:
        issued = await service.create_verification_token(USER_ID)
        await service.verify_email(issued.raw_token)

        with pytest.raises(InvalidVerificationToken) as raised:
            await service.verify_email(issued.raw_token)

        assert issued.raw_token not in raised.value.message


class TestOneTimeUse:
    async def test_a_redeemed_token_cannot_be_redeemed_again(
        self, service: EmailVerificationService
    ) -> None:
        """The replay guard. A link forwarded, or clicked twice, must not
        work the second time."""
        issued = await service.create_verification_token(USER_ID)
        await service.verify_email(issued.raw_token)

        with pytest.raises(InvalidVerificationToken):
            await service.verify_email(issued.raw_token)

    async def test_a_replay_does_not_call_the_verifier_again(
        self, service: EmailVerificationService, verifier: _RecordingVerifier
    ) -> None:
        """Consuming before writing is what makes this hold: the second
        request stops at the used token rather than reaching `users`."""
        issued = await service.create_verification_token(USER_ID)
        await service.verify_email(issued.raw_token)

        with pytest.raises(InvalidVerificationToken):
            await service.verify_email(issued.raw_token)

        assert verifier.verified == [USER_ID]

    async def test_redemption_invalidates_everything_outstanding(
        self,
        service: EmailVerificationService,
        repository: FakeVerificationTokenRepository,
    ) -> None:
        issued = await service.create_verification_token(USER_ID)

        await service.verify_email(issued.raw_token)

        assert await repository.count_active_for_user(USER_ID, at=NOW) == 0


class TestResendVerification:
    async def test_sends_a_link_for_an_unverified_account(
        self, service: EmailVerificationService, provider: _RecordingProvider
    ) -> None:
        await service.resend_verification(EMAIL)

        assert len(provider.sent) == 1
        assert provider.sent[0].to == EMAIL

    async def test_the_sent_link_actually_works(
        self, service: EmailVerificationService, provider: _RecordingProvider
    ) -> None:
        """Reads the token out of the message body and redeems it — the
        only assertion that proves the link a person receives is the one
        the database will accept."""
        await service.resend_verification(EMAIL)
        token = provider.sent[0].text_body.split("token=")[1].split()[0]

        assert (await service.verify_email(token)).is_verified is True

    async def test_an_unknown_address_does_not_raise(
        self, service: EmailVerificationService
    ) -> None:
        """The enumeration guard's first half: no exception. Propagating
        `UserNotFound` here *is* the leak — an exception is a branch."""
        await service.resend_verification("nobody@example.com")

    async def test_sends_nothing_for_an_unknown_address(
        self, service: EmailVerificationService, provider: _RecordingProvider
    ) -> None:
        await service.resend_verification("nobody@example.com")

        assert provider.sent == []

    async def test_an_already_verified_account_does_not_raise(
        self, service: EmailVerificationService, profiles: _FakeProfiles
    ) -> None:
        profiles.known = account(is_verified=True)

        await service.resend_verification(EMAIL)

    async def test_sends_nothing_for_an_already_verified_account(
        self,
        service: EmailVerificationService,
        profiles: _FakeProfiles,
        provider: _RecordingProvider,
    ) -> None:
        """Otherwise the endpoint mails a confirmed account on demand —
        a spam vector keyed on any address an attacker knows."""
        profiles.known = account(is_verified=True)

        await service.resend_verification(EMAIL)

        assert provider.sent == []

    async def test_the_caller_has_nothing_to_branch_on(
        self, service: EmailVerificationService
    ) -> None:
        """The enumeration guard's second half, asserted structurally
        rather than by comparing three `None`s — which mypy correctly
        calls a tautology.

        The *signature* is the guarantee: a method returning `None` cannot
        report which of the three outcomes happened, so no endpoint built
        on it can leak one. A later change to `bool` would be caught here
        before it reached a route.
        """
        import inspect

        signature = inspect.signature(service.resend_verification)

        assert signature.return_annotation in (None, "None")

    async def test_resending_invalidates_the_previous_link(
        self, service: EmailVerificationService, provider: _RecordingProvider
    ) -> None:
        await service.resend_verification(EMAIL)
        first = provider.sent[0].text_body.split("token=")[1].split()[0]

        await service.resend_verification(EMAIL)

        with pytest.raises(InvalidVerificationToken):
            await service.verify_email(first)

    async def test_the_newest_link_works_after_several_resends(
        self, service: EmailVerificationService, provider: _RecordingProvider
    ) -> None:
        for _ in range(3):
            await service.resend_verification(EMAIL)
        newest = provider.sent[-1].text_body.split("token=")[1].split()[0]

        assert (await service.verify_email(newest)).is_verified is True


class TestDelivery:
    async def test_the_message_carries_a_usable_link(
        self,
        service: EmailVerificationService,
        provider: _RecordingProvider,
        settings: EmailSettings,
    ) -> None:
        await service.send_verification(account())

        body = provider.sent[0].text_body
        assert settings.verification_url_template.split("{token}")[0] in body

    async def test_the_message_states_the_expiry(
        self, service: EmailVerificationService, provider: _RecordingProvider
    ) -> None:
        await service.send_verification(account())

        assert "24 hours" in provider.sent[0].text_body

    async def test_the_body_is_absent_from_the_repr(
        self, service: EmailVerificationService, provider: _RecordingProvider
    ) -> None:
        """The body *contains the raw token* — it is the one place that
        value legitimately exists in full."""
        await service.send_verification(account())

        assert provider.sent[0].text_body not in repr(provider.sent[0])

    async def test_a_send_failure_does_not_fail_the_request(
        self,
        repository: FakeVerificationTokenRepository,
        profiles: _FakeProfiles,
        verifier: _RecordingVerifier,
        unit_of_work: _NullUnitOfWork,
        clock: MovableClock,
        settings: EmailSettings,
    ) -> None:
        """A transient vendor outage must not turn a successful
        registration into a 500 — the token is committed and a resend
        produces a fresh one."""
        service = EmailVerificationService(
            tokens=repository,
            token_factory=OpaqueTokenService(settings.token_entropy_bytes),
            profiles=profiles,
            verifier=verifier,
            email=_BrokenProvider(),
            unit_of_work=unit_of_work,
            clock=clock,
            settings=settings,
        )

        await service.send_verification(account())

        assert await repository.count_active_for_user(USER_ID, at=NOW) == 1

    async def test_a_send_failure_is_logged_as_a_warning(
        self,
        repository: FakeVerificationTokenRepository,
        profiles: _FakeProfiles,
        verifier: _RecordingVerifier,
        unit_of_work: _NullUnitOfWork,
        clock: MovableClock,
        settings: EmailSettings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        service = EmailVerificationService(
            tokens=repository,
            token_factory=OpaqueTokenService(settings.token_entropy_bytes),
            profiles=profiles,
            verifier=verifier,
            email=_BrokenProvider(),
            unit_of_work=unit_of_work,
            clock=clock,
            settings=settings,
        )

        with caplog.at_level(logging.WARNING):
            await service.send_verification(account())

        assert "verification_email_send_failed" in caplog.text


class TestLogging:
    async def test_never_logs_the_raw_token(
        self, service: EmailVerificationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            issued = await service.create_verification_token(USER_ID)
            await service.verify_email(issued.raw_token)

        assert issued.raw_token not in caplog.text

    async def test_never_logs_the_address(
        self, service: EmailVerificationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An address in a log line is personal data in a system with
        broader read access and different retention than the database
        (services.md §8.5)."""
        with caplog.at_level(logging.DEBUG):
            await service.resend_verification(EMAIL)

        assert EMAIL not in caplog.text

    async def test_logs_a_successful_verification(
        self, service: EmailVerificationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        issued = await service.create_verification_token(USER_ID)

        with caplog.at_level(logging.INFO):
            await service.verify_email(issued.raw_token)

        record = next(r for r in caplog.records if r.message == "verification_succeeded")
        assert record.user_id == str(USER_ID)  # type: ignore[attr-defined]

    async def test_logs_a_failed_verification_with_its_reason(
        self, service: EmailVerificationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The distinction the response withholds is recorded server-side,
        where a caller cannot read it — a replayed link and a stale one
        are worth telling apart operationally."""
        issued = await service.create_verification_token(USER_ID)
        await service.verify_email(issued.raw_token)

        with caplog.at_level(logging.INFO), pytest.raises(InvalidVerificationToken):
            await service.verify_email(issued.raw_token)

        record = next(r for r in caplog.records if r.message == "verification_failed")
        assert record.reason == "used"  # type: ignore[attr-defined]

    async def test_logs_a_resend_request(
        self, service: EmailVerificationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            await service.resend_verification(EMAIL)

        assert "verification_resend_sent" in caplog.text

    async def test_logs_an_ignored_resend_without_the_address(
        self, service: EmailVerificationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown-address probes are exactly the traffic worth counting
        and exactly the traffic whose contents must not be retained."""
        with caplog.at_level(logging.INFO):
            await service.resend_verification("prober@example.com")

        assert "verification_resend_ignored" in caplog.text
        assert "prober@example.com" not in caplog.text


class TestTokenEntity:
    async def test_consume_is_idempotent_and_keeps_the_first_instant(
        self, service: EmailVerificationService
    ) -> None:
        from app.modules.auth.domain.verification import EmailVerificationToken

        token = EmailVerificationToken.issue(
            user_id=USER_ID,
            token_hash=b"\x00" * 32,
            issued_at=NOW,
            lifetime=timedelta(hours=24),
        )
        token.consume(NOW)

        token.consume(NOW + timedelta(hours=1))

        assert token.used_at == NOW

    def test_is_expired_at_exactly_expires_at(self) -> None:
        """`expires_at` is when it stops working, not the last instant it
        works — an off-by-one is a credential living an hour past its
        stated life."""
        from app.modules.auth.domain.verification import EmailVerificationToken

        token = EmailVerificationToken.issue(
            user_id=uuid4(),
            token_hash=b"\x00" * 32,
            issued_at=NOW,
            lifetime=timedelta(hours=24),
        )

        assert token.is_expired_at(NOW + timedelta(hours=24) - timedelta(seconds=1)) is False
        assert token.is_expired_at(NOW + timedelta(hours=24)) is True
