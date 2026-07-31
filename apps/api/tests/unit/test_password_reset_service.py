"""`PasswordResetService` — orchestration, no database.

Runs the real `OpaqueTokenService` (SHA-256 is microseconds, and the
properties under test are only real if the hashing is) and a **real
`SessionService`** over `FakeSessionRepository`, both of which the contract
suites in `tests/contract/` hold to the same behaviour as their SQLAlchemy
adapters.

The session service is real rather than a recording stub on purpose. "A
reset revokes every session" is the requirement most likely to pass
against a double and fail in production — a stub records the call, while
the real service is what proves the sessions are actually unusable
afterwards, which is what the tests below assert by trying to rotate them.

The password hasher is a stub. Real Argon2id is deliberately ~20ms per
call and this suite hashes on most tests; what is under test is
orchestration, and `tests/unit/test_password_hasher.py` already proves the
hasher hashes. `tests/contract/test_password_reset_api.py` runs the whole
flow through the real one.

The clock is movable, so "this link expired an hour ago" is an assignment
rather than a `sleep`.
"""

import logging
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.config.settings import EmailSettings, SessionSettings
from app.core.enums import Locale
from app.core.exceptions import ConflictError
from app.modules.auth.application.email import EmailMessage
from app.modules.auth.application.services import (
    OpaqueTokenService,
    PasswordResetService,
    RefreshTokenService,
    SessionService,
)
from app.modules.auth.domain.exceptions import (
    InvalidRefreshToken,
    InvalidResetToken,
    WeakPassword,
)
from app.modules.auth.domain.password_reset import PasswordResetToken
from app.modules.auth.domain.sessions import RevocationReason, SessionDevice
from app.modules.users.public import UserNotFound, UserRead
from tests.fakes.password_reset_token_repository import FakePasswordResetTokenRepository
from tests.fakes.session_repository import FakeSessionRepository

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
USER_ID = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")
EMAIL = "player.one@example.com"
NEW_PASSWORD = "BrandNewHorse1!"
OLD_HASH = "stub:OldHorse9?"


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


class _StubHasher:
    """`stub:{plaintext}` — instant, and obviously not Argon2 output so
    nothing can pass by coincidentally comparing against a real hash."""

    async def hash(self, plaintext: str) -> str:
        return f"stub:{plaintext}"

    async def verify(self, encoded_hash: str, plaintext: str) -> bool:
        return encoded_hash == f"stub:{plaintext}"

    async def needs_rehash(self, encoded_hash: str) -> bool:
        return False

    async def dummy_hash(self) -> str:
        return "stub:dummy"


def account(*, is_active: bool = True, is_verified: bool = True) -> UserRead:
    return UserRead(
        id=USER_ID,
        username="player_one",
        email=EMAIL,
        display_name=None,
        avatar_url=None,
        preferred_language=Locale.EN,
        timezone="UTC",
        is_active=is_active,
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
            raise UserNotFound("No such user.")
        return self.known

    async def find_by_email(self, email: str) -> UserRead | None:
        if self.known is None or self.known.email != email:
            return None
        return self.known


class _RecordingResetter:
    """`PasswordResetter`. Records the write rather than performing one, and
    keeps the stored hash so tests can assert what was actually saved."""

    def __init__(self, *, stored_hash: str = OLD_HASH, exists: bool = True) -> None:
        self.stored_hash = stored_hash
        self.calls: list[UUID] = []
        self._exists = exists

    async def reset_password(self, user_id: UUID, *, new_hash: str) -> None:
        if not self._exists:
            raise UserNotFound(f"No user with id {user_id}.")
        self.calls.append(user_id)
        self.stored_hash = new_hash


class _RecordingProvider:
    """`EmailProvider`. Keeps every message so tests can read the link out
    of the body — which is the only place the raw token legitimately exists
    once it has left the service."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _BrokenProvider:
    async def send(self, message: EmailMessage) -> None:
        raise RuntimeError("the mail provider is on fire")


class _ContendedRepository(FakePasswordResetTokenRepository):
    """A store whose `create` always loses the race.

    Stands in for the partial unique index rejecting an insert because a
    genuinely concurrent request for the same account got there first —
    which is the only way `ConflictError` reaches `forgot_password`, and
    is not otherwise reproducible in a single-threaded test.
    """

    async def create(self, token: PasswordResetToken) -> PasswordResetToken:
        raise ConflictError("Could not issue a password reset token.")


def link_token(message: EmailMessage) -> str:
    """The raw token, read out of the message body exactly as a person
    reading the email would."""
    return message.text_body.split("token=")[1].split()[0]


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def settings() -> EmailSettings:
    return EmailSettings()


@pytest.fixture
def repository() -> FakePasswordResetTokenRepository:
    return FakePasswordResetTokenRepository()


@pytest.fixture
def sessions_repository() -> FakeSessionRepository:
    return FakeSessionRepository()


@pytest.fixture
def unit_of_work() -> _NullUnitOfWork:
    return _NullUnitOfWork()


@pytest.fixture
def profiles() -> _FakeProfiles:
    return _FakeProfiles(account())


@pytest.fixture
def resetter() -> _RecordingResetter:
    return _RecordingResetter()


@pytest.fixture
def provider() -> _RecordingProvider:
    return _RecordingProvider()


@pytest.fixture
def sessions(
    sessions_repository: FakeSessionRepository,
    unit_of_work: _NullUnitOfWork,
    clock: MovableClock,
) -> SessionService:
    """The **real** `SessionService` — see this module's docstring."""
    session_settings = SessionSettings()
    return SessionService(
        sessions=sessions_repository,
        tokens=RefreshTokenService(session_settings),
        unit_of_work=unit_of_work,
        clock=clock,
        settings=session_settings,
    )


@pytest.fixture
def service(
    repository: FakePasswordResetTokenRepository,
    profiles: _FakeProfiles,
    resetter: _RecordingResetter,
    sessions: SessionService,
    provider: _RecordingProvider,
    unit_of_work: _NullUnitOfWork,
    clock: MovableClock,
    settings: EmailSettings,
) -> PasswordResetService:
    return PasswordResetService(
        tokens=repository,
        token_factory=OpaqueTokenService(settings.password_reset_token_entropy_bytes),
        profiles=profiles,
        resetter=resetter,
        password_hasher=_StubHasher(),
        sessions=sessions,
        email=provider,
        unit_of_work=unit_of_work,
        clock=clock,
        settings=settings,
    )


class TestCreateResetToken:
    async def test_returns_a_token_and_its_raw_value(self, service: PasswordResetService) -> None:
        issued = await service.create_reset_token(USER_ID)

        assert issued.token.user_id == USER_ID
        assert issued.raw_token

    async def test_stores_only_the_hash(
        self, service: PasswordResetService, settings: EmailSettings
    ) -> None:
        """§4.5: a database read "must not yield a working password
        reset"."""
        issued = await service.create_reset_token(USER_ID)

        factory = OpaqueTokenService(settings.password_reset_token_entropy_bytes)
        assert issued.token.token_hash == factory.hash(issued.raw_token)
        assert issued.raw_token.encode() not in issued.token.token_hash

    async def test_the_raw_token_is_absent_from_the_repr(
        self, service: PasswordResetService
    ) -> None:
        issued = await service.create_reset_token(USER_ID)

        assert issued.raw_token not in repr(issued)

    async def test_carries_256_bits(self, service: PasswordResetService) -> None:
        """DB-24's premise, and the task's floor. Below 256 bits, hashing
        with SHA-256 rather than Argon2id stops being sound."""
        issued = await service.create_reset_token(USER_ID)

        assert len(issued.raw_token) >= 43

    async def test_expires_in_one_hour(
        self, service: PasswordResetService, settings: EmailSettings
    ) -> None:
        """The task's figure, and shorter than verification's 24 hours on
        purpose — see `EmailSettings`."""
        issued = await service.create_reset_token(USER_ID)

        assert settings.password_reset_token_ttl_hours == 1
        assert issued.token.expires_at == NOW + timedelta(hours=1)

    async def test_commits(
        self, service: PasswordResetService, unit_of_work: _NullUnitOfWork
    ) -> None:
        await service.create_reset_token(USER_ID)

        assert unit_of_work.commits == 1

    async def test_two_issuances_produce_different_tokens(
        self, service: PasswordResetService
    ) -> None:
        first = await service.create_reset_token(USER_ID)
        second = await service.create_reset_token(USER_ID)

        assert first.raw_token != second.raw_token


class TestOnlyOneTokenIsEverLive:
    """database.md §4.5: "at most one live token per account"."""

    async def test_issuing_invalidates_the_previous_token(
        self, service: PasswordResetService
    ) -> None:
        first = await service.create_reset_token(USER_ID)

        await service.create_reset_token(USER_ID)

        with pytest.raises(InvalidResetToken):
            await service.reset_password(first.raw_token, NEW_PASSWORD)

    async def test_exactly_one_remains_active(
        self, service: PasswordResetService, repository: FakePasswordResetTokenRepository
    ) -> None:
        for _ in range(3):
            await service.create_reset_token(USER_ID)

        assert await repository.count_active_for_user(USER_ID, at=NOW) == 1

    async def test_the_newest_token_is_the_one_that_works(
        self, service: PasswordResetService, resetter: _RecordingResetter
    ) -> None:
        await service.create_reset_token(USER_ID)
        newest = await service.create_reset_token(USER_ID)

        await service.reset_password(newest.raw_token, NEW_PASSWORD)

        assert resetter.calls == [USER_ID]

    async def test_invalidate_previous_tokens_kills_the_live_one(
        self, service: PasswordResetService
    ) -> None:
        issued = await service.create_reset_token(USER_ID)

        assert await service.invalidate_previous_tokens(USER_ID) == 1

        with pytest.raises(InvalidResetToken):
            await service.reset_password(issued.raw_token, NEW_PASSWORD)

    async def test_invalidating_nothing_is_not_an_error(
        self, service: PasswordResetService
    ) -> None:
        assert await service.invalidate_previous_tokens(USER_ID) == 0


class TestSuccessfulReset:
    async def test_replaces_the_stored_hash(
        self, service: PasswordResetService, resetter: _RecordingResetter
    ) -> None:
        issued = await service.create_reset_token(USER_ID)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert resetter.calls == [USER_ID]
        assert resetter.stored_hash == f"stub:{NEW_PASSWORD}"

    async def test_the_new_hash_is_not_the_old_one(
        self, service: PasswordResetService, resetter: _RecordingResetter
    ) -> None:
        issued = await service.create_reset_token(USER_ID)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert resetter.stored_hash != OLD_HASH

    async def test_never_stores_the_plaintext(
        self, service: PasswordResetService, resetter: _RecordingResetter
    ) -> None:
        """The stub prefixes rather than hashes, so this asserts the
        service passed the password *through the hasher* rather than
        writing it straight to the column."""
        issued = await service.create_reset_token(USER_ID)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert resetter.stored_hash != NEW_PASSWORD

    async def test_returns_nothing(self, service: PasswordResetService) -> None:
        """A reset hands back no account and no token pair — the caller
        holds no credential afterwards and must sign in.

        Asserted on the *signature* rather than by comparing the result to
        `None`, which mypy correctly calls a tautology. The signature is
        the guarantee: a method returning `None` cannot hand an endpoint a
        session to return, so no route built on it can turn control of an
        inbox into a live credential by accident.
        """
        import inspect

        signature = inspect.signature(service.reset_password)

        assert signature.return_annotation in (None, "None")

    async def test_works_up_to_the_last_second(
        self,
        service: PasswordResetService,
        clock: MovableClock,
        resetter: _RecordingResetter,
    ) -> None:
        issued = await service.create_reset_token(USER_ID)
        clock.instant = NOW + timedelta(hours=1) - timedelta(seconds=1)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert resetter.calls == [USER_ID]

    async def test_a_deleted_account_raises_rather_than_silently_succeeding(
        self,
        repository: FakePasswordResetTokenRepository,
        profiles: _FakeProfiles,
        sessions: SessionService,
        provider: _RecordingProvider,
        unit_of_work: _NullUnitOfWork,
        clock: MovableClock,
        settings: EmailSettings,
    ) -> None:
        """The narrow window in which an account is deleted between the
        link being issued and clicked. A 404 is the honest answer; a
        silent success would tell somebody their password had changed
        when no row was written."""
        service = PasswordResetService(
            tokens=repository,
            token_factory=OpaqueTokenService(settings.password_reset_token_entropy_bytes),
            profiles=profiles,
            resetter=_RecordingResetter(exists=False),
            password_hasher=_StubHasher(),
            sessions=sessions,
            email=provider,
            unit_of_work=unit_of_work,
            clock=clock,
            settings=settings,
        )
        issued = await service.create_reset_token(USER_ID)

        with pytest.raises(UserNotFound):
            await service.reset_password(issued.raw_token, NEW_PASSWORD)


class TestExpiredToken:
    async def test_rejects_an_expired_token(
        self, service: PasswordResetService, clock: MovableClock
    ) -> None:
        issued = await service.create_reset_token(USER_ID)
        clock.instant = NOW + timedelta(hours=1)

        with pytest.raises(InvalidResetToken):
            await service.reset_password(issued.raw_token, NEW_PASSWORD)

    async def test_an_expired_token_does_not_replace_the_password(
        self,
        service: PasswordResetService,
        resetter: _RecordingResetter,
        clock: MovableClock,
    ) -> None:
        issued = await service.create_reset_token(USER_ID)
        clock.instant = NOW + timedelta(days=1)

        with pytest.raises(InvalidResetToken):
            await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert resetter.calls == []
        assert resetter.stored_hash == OLD_HASH

    async def test_an_expired_token_does_not_revoke_sessions(
        self,
        service: PasswordResetService,
        sessions: SessionService,
        clock: MovableClock,
    ) -> None:
        live = await sessions.create_session(USER_ID, device=SessionDevice())
        issued = await service.create_reset_token(USER_ID)
        clock.instant = NOW + timedelta(days=1)

        with pytest.raises(InvalidResetToken):
            await service.reset_password(issued.raw_token, NEW_PASSWORD)

        # Still rotatable, i.e. still a working session. Asserted by using
        # it rather than by reading a flag.
        clock.instant = NOW
        assert await sessions.rotate_refresh_token(live.refresh_token)

    async def test_expiry_is_exclusive_at_the_boundary(self) -> None:
        """`expires_at` is when the link stops working, not the last
        instant it works — an off-by-one is a credential living an hour
        past its stated life."""
        token = PasswordResetToken.issue(
            user_id=uuid4(),
            token_hash=b"\x00" * 32,
            issued_at=NOW,
            lifetime=timedelta(hours=1),
        )

        assert token.is_expired_at(NOW + timedelta(hours=1) - timedelta(seconds=1)) is False
        assert token.is_expired_at(NOW + timedelta(hours=1)) is True


class TestInvalidToken:
    async def test_rejects_an_unknown_token(self, service: PasswordResetService) -> None:
        with pytest.raises(InvalidResetToken):
            await service.reset_password("a-token-nobody-ever-issued", NEW_PASSWORD)

    async def test_rejects_an_empty_token(self, service: PasswordResetService) -> None:
        with pytest.raises(InvalidResetToken):
            await service.reset_password("", NEW_PASSWORD)

    async def test_an_unknown_token_does_not_replace_the_password(
        self, service: PasswordResetService, resetter: _RecordingResetter
    ) -> None:
        with pytest.raises(InvalidResetToken):
            await service.reset_password("nope", NEW_PASSWORD)

        assert resetter.calls == []

    async def test_a_verification_token_is_not_a_reset_token(
        self, service: PasswordResetService
    ) -> None:
        """The two credentials live in different tables and are different
        types. A link that confirms an address must never replace a
        password — here that is enforced by the reset repository simply
        not containing the row."""
        from app.modules.auth.domain.verification import EmailVerificationToken

        verification = EmailVerificationToken.issue(
            user_id=USER_ID,
            token_hash=OpaqueTokenService().hash("a-verification-token"),
            issued_at=NOW,
            lifetime=timedelta(hours=24),
        )
        assert verification.is_usable_at(NOW)

        with pytest.raises(InvalidResetToken):
            await service.reset_password("a-verification-token", NEW_PASSWORD)

    async def test_every_failure_carries_the_same_message_and_code(
        self, service: PasswordResetService, clock: MovableClock
    ) -> None:
        """Unknown, used and expired are indistinguishable to a caller —
        the action is the same in all three, and separating them says
        whether a token the caller holds was ever real."""
        used = await service.create_reset_token(USER_ID)
        await service.reset_password(used.raw_token, NEW_PASSWORD)
        expired = await service.create_reset_token(USER_ID)
        clock.instant = NOW + timedelta(days=1)

        messages = set()
        codes = set()
        for candidate in (used.raw_token, expired.raw_token, "never-issued"):
            with pytest.raises(InvalidResetToken) as raised:
                await service.reset_password(candidate, NEW_PASSWORD)
            messages.add(raised.value.message)
            codes.add(raised.value.code)

        assert len(messages) == 1
        assert len(codes) == 1

    async def test_the_message_never_contains_the_token(
        self, service: PasswordResetService
    ) -> None:
        issued = await service.create_reset_token(USER_ID)
        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        with pytest.raises(InvalidResetToken) as raised:
            await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert issued.raw_token not in raised.value.message


class TestTokenReuse:
    async def test_a_redeemed_token_cannot_be_redeemed_again(
        self, service: PasswordResetService
    ) -> None:
        """The replay guard. A link forwarded, or a form double-submitted,
        must not work the second time."""
        issued = await service.create_reset_token(USER_ID)
        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        with pytest.raises(InvalidResetToken):
            await service.reset_password(issued.raw_token, "SecondAttempt2!")

    async def test_a_replay_does_not_write_a_second_password(
        self, service: PasswordResetService, resetter: _RecordingResetter
    ) -> None:
        """Consuming before writing is what makes this hold: the second
        request stops at the used token rather than reaching `users`. If it
        did not, an attacker replaying a captured link could overwrite the
        password the legitimate owner just set."""
        issued = await service.create_reset_token(USER_ID)
        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        with pytest.raises(InvalidResetToken):
            await service.reset_password(issued.raw_token, "AttackerChoice3!")

        assert resetter.calls == [USER_ID]
        assert resetter.stored_hash == f"stub:{NEW_PASSWORD}"

    async def test_a_reset_invalidates_every_remaining_token(
        self, service: PasswordResetService, repository: FakePasswordResetTokenRepository
    ) -> None:
        issued = await service.create_reset_token(USER_ID)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert await repository.count_active_for_user(USER_ID, at=NOW) == 0

    async def test_the_first_consumption_instant_is_kept(
        self, service: PasswordResetService
    ) -> None:
        """A replay must not move `used_at` — that instant is the only
        record of when the real redemption happened."""
        token = PasswordResetToken.issue(
            user_id=USER_ID,
            token_hash=b"\x00" * 32,
            issued_at=NOW,
            lifetime=timedelta(hours=1),
        )
        token.consume(NOW)

        token.consume(NOW + timedelta(minutes=30))

        assert token.used_at == NOW


class TestSessionInvalidation:
    async def test_a_reset_revokes_every_session(
        self, service: PasswordResetService, sessions: SessionService
    ) -> None:
        for _ in range(3):
            await sessions.create_session(USER_ID, device=SessionDevice())
        issued = await service.create_reset_token(USER_ID)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert await sessions.list_user_sessions(USER_ID) == []

    async def test_the_revoked_refresh_tokens_actually_stop_working(
        self, service: PasswordResetService, sessions: SessionService
    ) -> None:
        """The assertion that matters. A revocation flag nothing checks is
        not a revocation — this proves the credential is refused."""
        phone = await sessions.create_session(USER_ID, device=SessionDevice())
        laptop = await sessions.create_session(USER_ID, device=SessionDevice())
        issued = await service.create_reset_token(USER_ID)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        for revoked in (phone, laptop):
            with pytest.raises(InvalidRefreshToken):
                await sessions.rotate_refresh_token(revoked.refresh_token)

    async def test_no_session_is_spared(
        self, service: PasswordResetService, sessions: SessionService
    ) -> None:
        """SE-1's `except_session_id` exists for an *authenticated*
        password change. A reset has no performing session, and the
        plausible reason somebody is here is that another party holds
        one."""
        issued_session = await sessions.create_session(USER_ID, device=SessionDevice())
        issued = await service.create_reset_token(USER_ID)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        stored = await sessions.list_user_sessions(USER_ID, include_revoked=True)
        assert [session.id for session in stored] == [issued_session.session.id]
        assert all(session.is_revoked for session in stored)

    async def test_the_recorded_reason_is_password_change(
        self, service: PasswordResetService, sessions: SessionService
    ) -> None:
        await sessions.create_session(USER_ID, device=SessionDevice())
        issued = await service.create_reset_token(USER_ID)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        stored = await sessions.list_user_sessions(USER_ID, include_revoked=True)
        assert stored[0].revoked_reason is RevocationReason.PASSWORD_CHANGE

    async def test_another_accounts_sessions_are_untouched(
        self, service: PasswordResetService, sessions: SessionService
    ) -> None:
        """Blast radius. A reset is scoped to one account, and the obvious
        implementation mistake — revoking by nothing at all — would sign
        the whole platform out."""
        other_user = uuid4()
        theirs = await sessions.create_session(other_user, device=SessionDevice())
        issued = await service.create_reset_token(USER_ID)

        await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert await sessions.rotate_refresh_token(theirs.refresh_token)

    async def test_invalidate_all_sessions_is_callable_on_its_own(
        self, service: PasswordResetService, sessions: SessionService
    ) -> None:
        await sessions.create_session(USER_ID, device=SessionDevice())
        await sessions.create_session(USER_ID, device=SessionDevice())

        assert await service.invalidate_all_sessions(USER_ID) == 2

    async def test_invalidating_no_sessions_is_not_an_error(
        self, service: PasswordResetService
    ) -> None:
        assert await service.invalidate_all_sessions(USER_ID) == 0


class TestPasswordPolicy:
    @pytest.mark.parametrize(
        ("password", "unmet_rule"),
        [
            ("Ab1!", "at least 8 characters"),
            ("A" * 129 + "b1!", "at most 128 characters"),
            ("lowercase1!", "uppercase letter"),
            ("UPPERCASE1!", "lowercase letter"),
            ("NoDigitsHere!", "digit"),
            ("NoSpecials123", "special character"),
        ],
    )
    async def test_rejects_a_password_that_fails_the_policy(
        self, service: PasswordResetService, password: str, unmet_rule: str
    ) -> None:
        issued = await service.create_reset_token(USER_ID)

        with pytest.raises(WeakPassword) as raised:
            await service.reset_password(issued.raw_token, password)

        assert unmet_rule in raised.value.message

    async def test_the_policy_message_never_contains_the_password(
        self, service: PasswordResetService
    ) -> None:
        issued = await service.create_reset_token(USER_ID)

        with pytest.raises(WeakPassword) as raised:
            await service.reset_password(issued.raw_token, "weak")

        assert "weak" not in raised.value.message

    async def test_a_weak_password_does_not_consume_the_token(
        self, service: PasswordResetService, resetter: _RecordingResetter
    ) -> None:
        """Kindness, and a security property. Somebody who fumbles their
        new password gets to try again with the same link rather than
        finding their one-time token burned by a typo."""
        issued = await service.create_reset_token(USER_ID)

        with pytest.raises(WeakPassword):
            await service.reset_password(issued.raw_token, "weak")

        await service.reset_password(issued.raw_token, NEW_PASSWORD)
        assert resetter.calls == [USER_ID]

    async def test_a_weak_password_does_not_revoke_sessions(
        self, service: PasswordResetService, sessions: SessionService
    ) -> None:
        live = await sessions.create_session(USER_ID, device=SessionDevice())
        issued = await service.create_reset_token(USER_ID)

        with pytest.raises(WeakPassword):
            await service.reset_password(issued.raw_token, "weak")

        assert await sessions.rotate_refresh_token(live.refresh_token)

    async def test_the_policy_is_checked_before_the_token(
        self, service: PasswordResetService
    ) -> None:
        """The oracle guard, and the reason for the check order.

        If the token were validated first, an attacker holding a candidate
        token could submit it with a deliberately awful password and read
        the answer off the exception type: `WeakPassword` would mean "that
        token is real and still unconsumed", `InvalidResetToken` would mean
        it is not. Checking the policy first makes both cases identical.
        """
        with pytest.raises(WeakPassword):
            await service.reset_password("a-token-nobody-ever-issued", "weak")

    async def test_the_same_policy_registration_enforces(self) -> None:
        """One definition, not two. A reset endpoint accepting a password
        registration would refuse is how a platform ends up with a weaker
        policy reachable by anyone with an inbox."""
        import inspect

        from app.modules.auth.application.services import password_reset_service

        source = inspect.getsource(password_reset_service)
        assert "validate_password" in source


class TestForgotPassword:
    async def test_sends_a_link_for_a_known_address(
        self, service: PasswordResetService, provider: _RecordingProvider
    ) -> None:
        await service.forgot_password(EMAIL)

        assert len(provider.sent) == 1
        assert provider.sent[0].to == EMAIL

    async def test_the_sent_link_actually_works(
        self,
        service: PasswordResetService,
        provider: _RecordingProvider,
        resetter: _RecordingResetter,
    ) -> None:
        """Reads the token out of the message body and redeems it — the
        only assertion that proves the link a person receives is the one
        the database will accept."""
        await service.forgot_password(EMAIL)

        await service.reset_password(link_token(provider.sent[0]), NEW_PASSWORD)

        assert resetter.stored_hash == f"stub:{NEW_PASSWORD}"

    async def test_an_unknown_address_does_not_raise(self, service: PasswordResetService) -> None:
        """The enumeration guard's first half: no exception. Propagating
        `UserNotFound` here *is* the leak — an exception is a branch."""
        await service.forgot_password("nobody@example.com")

    async def test_sends_nothing_for_an_unknown_address(
        self, service: PasswordResetService, provider: _RecordingProvider
    ) -> None:
        await service.forgot_password("nobody@example.com")

        assert provider.sent == []

    async def test_issues_nothing_for_an_unknown_address(
        self, service: PasswordResetService, repository: FakePasswordResetTokenRepository
    ) -> None:
        await service.forgot_password("nobody@example.com")

        assert await repository.count_active_for_user(USER_ID, at=NOW) == 0

    async def test_a_deactivated_account_gets_nothing(
        self,
        service: PasswordResetService,
        profiles: _FakeProfiles,
        provider: _RecordingProvider,
    ) -> None:
        """Somebody who cannot sign in must not have their credential
        rotated by a stranger who knows their address, and a reset that
        "succeeds" into an account that still cannot sign in helps
        nobody."""
        profiles.known = account(is_active=False)

        await service.forgot_password(EMAIL)

        assert provider.sent == []

    async def test_a_deactivated_account_does_not_raise(
        self, service: PasswordResetService, profiles: _FakeProfiles
    ) -> None:
        profiles.known = account(is_active=False)

        await service.forgot_password(EMAIL)

    async def test_an_unverified_account_still_gets_a_link(
        self,
        service: PasswordResetService,
        profiles: _FakeProfiles,
        provider: _RecordingProvider,
    ) -> None:
        """The opposite call to deactivation, and the right one: the
        address on file is the one the account was registered with, so
        nothing new is being trusted. Refusing would strand anybody who
        registered, never verified, and then forgot their password."""
        profiles.known = account(is_verified=False)

        await service.forgot_password(EMAIL)

        assert len(provider.sent) == 1

    async def test_a_concurrent_request_does_not_raise(
        self,
        profiles: _FakeProfiles,
        resetter: _RecordingResetter,
        sessions: SessionService,
        provider: _RecordingProvider,
        unit_of_work: _NullUnitOfWork,
        clock: MovableClock,
        settings: EmailSettings,
    ) -> None:
        """The enumeration guard's third case, and the least obvious one.

        `ConflictError` is a 409, and an unknown address can never produce
        one — it returns before touching storage. So a conflict escaping
        would make the endpoint answer 409 for accounts that exist and 204
        for accounts that do not, handing the oracle back to anyone willing
        to send two requests at once.
        """
        service = PasswordResetService(
            tokens=_ContendedRepository(),
            token_factory=OpaqueTokenService(settings.password_reset_token_entropy_bytes),
            profiles=profiles,
            resetter=resetter,
            password_hasher=_StubHasher(),
            sessions=sessions,
            email=provider,
            unit_of_work=unit_of_work,
            clock=clock,
            settings=settings,
        )

        await service.forgot_password(EMAIL)

        # And sends nothing, because the request that won the race has
        # already delivered a link to the same inbox.
        assert provider.sent == []

    async def test_a_concurrent_request_is_logged(
        self,
        profiles: _FakeProfiles,
        resetter: _RecordingResetter,
        sessions: SessionService,
        provider: _RecordingProvider,
        unit_of_work: _NullUnitOfWork,
        clock: MovableClock,
        settings: EmailSettings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Swallowed for the caller, not for the operator — a rate of these
        means something is hammering the endpoint, which is exactly what
        A64-011.8 wants to see."""
        service = PasswordResetService(
            tokens=_ContendedRepository(),
            token_factory=OpaqueTokenService(settings.password_reset_token_entropy_bytes),
            profiles=profiles,
            resetter=resetter,
            password_hasher=_StubHasher(),
            sessions=sessions,
            email=provider,
            unit_of_work=unit_of_work,
            clock=clock,
            settings=settings,
        )

        with caplog.at_level(logging.WARNING):
            await service.forgot_password(EMAIL)

        record = next(r for r in caplog.records if r.message == "password_reset_requested")
        assert record.outcome == "concurrent_request"  # type: ignore[attr-defined]

    async def test_the_caller_has_nothing_to_branch_on(self, service: PasswordResetService) -> None:
        """The enumeration guard's second half, asserted structurally
        rather than by comparing three `None`s — which mypy correctly calls
        a tautology.

        The *signature* is the guarantee: a method returning `None` cannot
        report which of the three outcomes happened, so no endpoint built
        on it can leak one. A later change to `bool` would be caught here
        before it reached a route.
        """
        import inspect

        signature = inspect.signature(service.forgot_password)

        assert signature.return_annotation in (None, "None")

    async def test_asking_twice_invalidates_the_first_link(
        self, service: PasswordResetService, provider: _RecordingProvider
    ) -> None:
        await service.forgot_password(EMAIL)
        first = link_token(provider.sent[0])

        await service.forgot_password(EMAIL)

        with pytest.raises(InvalidResetToken):
            await service.reset_password(first, NEW_PASSWORD)

    async def test_the_newest_link_works_after_several_requests(
        self,
        service: PasswordResetService,
        provider: _RecordingProvider,
        resetter: _RecordingResetter,
    ) -> None:
        for _ in range(3):
            await service.forgot_password(EMAIL)

        await service.reset_password(link_token(provider.sent[-1]), NEW_PASSWORD)

        assert resetter.calls == [USER_ID]


class TestDelivery:
    async def test_the_message_carries_a_usable_link(
        self, service: PasswordResetService, provider: _RecordingProvider, settings: EmailSettings
    ) -> None:
        await service.forgot_password(EMAIL)

        body = provider.sent[0].text_body
        assert settings.password_reset_url_template.split("{token}")[0] in body

    async def test_the_link_is_the_reset_page_not_the_verification_page(
        self, service: PasswordResetService, provider: _RecordingProvider
    ) -> None:
        """A copy-paste of the verification template would send people to
        a page that cannot reset anything, and would do it silently."""
        await service.forgot_password(EMAIL)

        assert "reset-password" in provider.sent[0].text_body
        assert "verify-email" not in provider.sent[0].text_body

    async def test_the_message_states_the_expiry(
        self, service: PasswordResetService, provider: _RecordingProvider
    ) -> None:
        await service.forgot_password(EMAIL)

        assert "1 hour" in provider.sent[0].text_body

    async def test_the_message_tells_an_unwitting_recipient_to_do_nothing(
        self, service: PasswordResetService, provider: _RecordingProvider
    ) -> None:
        """A reset email arriving unrequested is the first thing somebody
        sees when an attacker is probing their account. The correct advice
        is genuinely "do nothing" — saying so stops a worried person from
        clicking the link to "check", which would consume their token for
        the attacker's benefit."""
        await service.forgot_password(EMAIL)

        body = provider.sent[0].text_body
        assert "did not ask for this" in body
        assert "has not changed" in body

    async def test_the_body_is_absent_from_the_repr(
        self, service: PasswordResetService, provider: _RecordingProvider
    ) -> None:
        """The body *contains the raw token* — it is the one place that
        value legitimately exists in full."""
        await service.forgot_password(EMAIL)

        assert provider.sent[0].text_body not in repr(provider.sent[0])

    async def test_a_send_failure_does_not_raise(
        self,
        repository: FakePasswordResetTokenRepository,
        profiles: _FakeProfiles,
        resetter: _RecordingResetter,
        sessions: SessionService,
        unit_of_work: _NullUnitOfWork,
        clock: MovableClock,
        settings: EmailSettings,
    ) -> None:
        """Load-bearing for the enumeration guard, not merely robustness.
        An unknown address never reaches the provider and so can never
        fail; if a *known* address could 500 on a vendor outage, the
        endpoint would answer 500 for accounts that exist and 204 for
        accounts that do not."""
        service = PasswordResetService(
            tokens=repository,
            token_factory=OpaqueTokenService(settings.password_reset_token_entropy_bytes),
            profiles=profiles,
            resetter=resetter,
            password_hasher=_StubHasher(),
            sessions=sessions,
            email=_BrokenProvider(),
            unit_of_work=unit_of_work,
            clock=clock,
            settings=settings,
        )

        await service.forgot_password(EMAIL)

    async def test_a_send_failure_is_logged_as_a_warning(
        self,
        repository: FakePasswordResetTokenRepository,
        profiles: _FakeProfiles,
        resetter: _RecordingResetter,
        sessions: SessionService,
        unit_of_work: _NullUnitOfWork,
        clock: MovableClock,
        settings: EmailSettings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        service = PasswordResetService(
            tokens=repository,
            token_factory=OpaqueTokenService(settings.password_reset_token_entropy_bytes),
            profiles=profiles,
            resetter=resetter,
            password_hasher=_StubHasher(),
            sessions=sessions,
            email=_BrokenProvider(),
            unit_of_work=unit_of_work,
            clock=clock,
            settings=settings,
        )

        with caplog.at_level(logging.WARNING):
            await service.forgot_password(EMAIL)

        assert "password_reset_email_send_failed" in caplog.text


class TestLogging:
    async def test_never_logs_the_raw_token(
        self, service: PasswordResetService, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            issued = await service.create_reset_token(USER_ID)
            await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert issued.raw_token not in caplog.text

    async def test_never_logs_the_new_password(
        self, service: PasswordResetService, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            issued = await service.create_reset_token(USER_ID)
            await service.reset_password(issued.raw_token, NEW_PASSWORD)

        assert NEW_PASSWORD not in caplog.text

    async def test_never_logs_a_rejected_password(
        self, service: PasswordResetService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The path most likely to leak one: an error carrying the value
        that caused it."""
        with caplog.at_level(logging.DEBUG), pytest.raises(WeakPassword):
            await service.reset_password("whatever", "hunter2")

        assert "hunter2" not in caplog.text

    async def test_never_logs_the_address(
        self, service: PasswordResetService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An address in a log line is personal data in a system with
        broader read access and different retention than the database
        (services.md §8.5)."""
        with caplog.at_level(logging.DEBUG):
            await service.forgot_password(EMAIL)

        assert EMAIL not in caplog.text

    async def test_logs_a_reset_request(
        self, service: PasswordResetService, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            await service.forgot_password(EMAIL)

        record = next(r for r in caplog.records if r.message == "password_reset_requested")
        assert record.outcome == "link_sent"  # type: ignore[attr-defined]

    async def test_logs_an_ignored_request_without_the_address(
        self, service: PasswordResetService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown-address probes are exactly the traffic worth counting
        and exactly the traffic whose contents must not be retained. This
        line is what A64-011.8's alerting will key on."""
        with caplog.at_level(logging.INFO):
            await service.forgot_password("prober@example.com")

        record = next(r for r in caplog.records if r.message == "password_reset_requested")
        assert record.outcome == "no_account"  # type: ignore[attr-defined]
        assert "prober@example.com" not in caplog.text

    async def test_logs_a_successful_reset(
        self, service: PasswordResetService, caplog: pytest.LogCaptureFixture
    ) -> None:
        issued = await service.create_reset_token(USER_ID)

        with caplog.at_level(logging.INFO):
            await service.reset_password(issued.raw_token, NEW_PASSWORD)

        record = next(r for r in caplog.records if r.message == "password_reset_succeeded")
        assert record.user_id == str(USER_ID)  # type: ignore[attr-defined]

    async def test_a_successful_reset_records_what_it_invalidated(
        self,
        service: PasswordResetService,
        sessions: SessionService,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await sessions.create_session(USER_ID, device=SessionDevice())
        await sessions.create_session(USER_ID, device=SessionDevice())
        issued = await service.create_reset_token(USER_ID)

        with caplog.at_level(logging.INFO):
            await service.reset_password(issued.raw_token, NEW_PASSWORD)

        record = next(r for r in caplog.records if r.message == "password_reset_succeeded")
        assert record.sessions_revoked == 2  # type: ignore[attr-defined]

    @pytest.mark.parametrize("reason", ["used", "expired", "unknown_token"])
    async def test_logs_an_invalid_attempt_with_its_reason(
        self,
        service: PasswordResetService,
        clock: MovableClock,
        caplog: pytest.LogCaptureFixture,
        reason: str,
    ) -> None:
        """The distinction the response withholds is recorded server-side,
        where a caller cannot read it — a replayed link and a stale one are
        worth telling apart operationally."""
        if reason == "unknown_token":
            candidate = "never-issued"
        else:
            issued = await service.create_reset_token(USER_ID)
            candidate = issued.raw_token
            if reason == "used":
                await service.reset_password(candidate, NEW_PASSWORD)
            else:
                clock.instant = NOW + timedelta(days=1)

        with caplog.at_level(logging.INFO), pytest.raises(InvalidResetToken):
            await service.reset_password(candidate, NEW_PASSWORD)

        record = next(r for r in reversed(caplog.records) if r.message == "password_reset_failed")
        assert record.reason == reason  # type: ignore[attr-defined]
