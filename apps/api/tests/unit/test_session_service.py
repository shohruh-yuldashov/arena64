"""`SessionService` — orchestration, no database.

Runs the real `RefreshTokenService` (SHA-256 is microseconds, so there is
nothing to gain from faking it, and the properties under test are only
real if the hashing is) against `FakeSessionRepository`, which the
contract suite in `tests/contract/test_session_repository.py` holds to the
same behaviour as the SQLAlchemy adapter.

The clock is movable, so "this session expired three weeks from now" is
an assignment rather than a `sleep`.
"""

import logging
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

import pytest

from app.config.settings import SessionSettings
from app.modules.auth.application.services import (
    RefreshTokenService,
    SessionService,
)
from app.modules.auth.domain.exceptions import (
    ExpiredRefreshToken,
    InvalidRefreshToken,
    RevokedSession,
    SessionNotFound,
)
from app.modules.auth.domain.sessions import RevocationReason, SessionDevice, UserSession
from tests.fakes.session_repository import FakeSessionRepository

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
USER_ID = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")
OTHER_USER_ID = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a97")


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


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def settings() -> SessionSettings:
    return SessionSettings()


@pytest.fixture
def repository() -> FakeSessionRepository:
    return FakeSessionRepository()


@pytest.fixture
def unit_of_work() -> _NullUnitOfWork:
    return _NullUnitOfWork()


@pytest.fixture
def service(
    repository: FakeSessionRepository,
    unit_of_work: _NullUnitOfWork,
    clock: MovableClock,
    settings: SessionSettings,
) -> SessionService:
    return SessionService(
        sessions=repository,
        tokens=RefreshTokenService(settings),
        unit_of_work=unit_of_work,
        clock=clock,
        settings=settings,
    )


class TestCreateSession:
    async def test_returns_the_session_and_its_raw_token(self, service: SessionService) -> None:
        issued = await service.create_session(USER_ID)

        assert issued.session.user_id == USER_ID
        assert issued.refresh_token

    async def test_stores_only_the_hash(
        self, service: SessionService, repository: FakeSessionRepository, settings: SessionSettings
    ) -> None:
        """§14.3: "the token itself exists only in transit and in the
        client". A database read must not yield a working credential."""
        issued = await service.create_session(USER_ID)
        stored = await repository.get_by_id(issued.session.id)

        assert stored is not None
        assert stored.refresh_token_hash == RefreshTokenService(settings).hash_refresh_token(
            issued.refresh_token
        )
        assert issued.refresh_token.encode() not in stored.refresh_token_hash

    async def test_the_raw_token_is_absent_from_the_repr(self, service: SessionService) -> None:
        """A dataclass repr lands in tracebacks and in every error reporter
        that walks frame locals — a refresh token in a bug report is a
        thirty-day credential."""
        issued = await service.create_session(USER_ID)

        assert issued.refresh_token not in repr(issued)

    async def test_applies_the_configured_lifetime(
        self, service: SessionService, settings: SessionSettings
    ) -> None:
        issued = await service.create_session(USER_ID)

        assert issued.session.expires_at == NOW + timedelta(days=settings.refresh_token_ttl_days)

    async def test_commits(self, service: SessionService, unit_of_work: _NullUnitOfWork) -> None:
        await service.create_session(USER_ID)

        assert unit_of_work.commits == 1

    async def test_records_the_device(self, service: SessionService) -> None:
        device = SessionDevice(
            device_name="Chrome on macOS",
            user_agent="Mozilla/5.0",
            ip_address="203.0.113.7",
        )

        issued = await service.create_session(USER_ID, device=device)

        assert issued.session.device == device

    async def test_each_sign_in_starts_a_new_family(self, service: SessionService) -> None:
        """What makes multiple devices independent: reuse detection on the
        phone's chain must not sign the laptop out."""
        first = await service.create_session(USER_ID)
        second = await service.create_session(USER_ID)

        assert first.session.token_family != second.session.token_family

    async def test_two_sessions_get_different_tokens(self, service: SessionService) -> None:
        first = await service.create_session(USER_ID)
        second = await service.create_session(USER_ID)

        assert first.refresh_token != second.refresh_token

    async def test_multiple_devices_coexist(self, service: SessionService) -> None:
        """The task's first stated requirement. Nothing evicts an earlier
        session when a new one is created."""
        await service.create_session(USER_ID)
        await service.create_session(USER_ID)
        await service.create_session(USER_ID)

        assert len(await service.list_user_sessions(USER_ID)) == 3


class TestCreationLogging:
    async def test_logs_identifiers_only(
        self, service: SessionService, caplog: pytest.LogCaptureFixture
    ) -> None:
        device = SessionDevice(
            device_name="Chrome", user_agent="Mozilla/5.0", ip_address="203.0.113.7"
        )
        with caplog.at_level(logging.DEBUG):
            issued = await service.create_session(USER_ID, device=device)

        record = next(r for r in caplog.records if r.message == "session_created")
        assert record.session_id == str(issued.session.id)  # type: ignore[attr-defined]

    async def test_never_logs_the_token_or_personal_data(
        self, service: SessionService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The token is a credential; the user agent and IP are Personal
        data under database.md §14.1, and a log line is a permanent record
        with broader read access than the database."""
        device = SessionDevice(
            device_name="Chrome", user_agent="Mozilla/5.0 Special", ip_address="203.0.113.7"
        )
        with caplog.at_level(logging.DEBUG):
            issued = await service.create_session(USER_ID, device=device)

        assert issued.refresh_token not in caplog.text
        assert "Mozilla/5.0 Special" not in caplog.text
        assert "203.0.113.7" not in caplog.text


class TestValidateRefreshToken:
    async def test_accepts_a_freshly_issued_token(self, service: SessionService) -> None:
        issued = await service.create_session(USER_ID)

        validated = await service.validate_refresh_token(issued.refresh_token)

        assert validated.id == issued.session.id

    async def test_rejects_an_unknown_token(self, service: SessionService) -> None:
        with pytest.raises(SessionNotFound):
            await service.validate_refresh_token("a-token-nobody-ever-issued")

    async def test_rejects_an_empty_token(self, service: SessionService) -> None:
        with pytest.raises(SessionNotFound):
            await service.validate_refresh_token("")

    async def test_rejects_a_token_from_a_different_session(self, service: SessionService) -> None:
        """Two live sessions; each token must open only its own."""
        first = await service.create_session(USER_ID)
        second = await service.create_session(USER_ID)

        assert (await service.validate_refresh_token(first.refresh_token)).id == first.session.id
        assert (await service.validate_refresh_token(second.refresh_token)).id == second.session.id

    async def test_rejects_an_expired_session(
        self, service: SessionService, clock: MovableClock, settings: SessionSettings
    ) -> None:
        issued = await service.create_session(USER_ID)
        clock.instant = NOW + timedelta(days=settings.refresh_token_ttl_days)

        with pytest.raises(ExpiredRefreshToken):
            await service.validate_refresh_token(issued.refresh_token)

    async def test_accepts_one_second_before_absolute_expiry(
        self,
        service: SessionService,
        repository: FakeSessionRepository,
        clock: MovableClock,
        settings: SessionSettings,
    ) -> None:
        issued = await service.create_session(USER_ID)
        clock.instant = NOW + timedelta(days=settings.refresh_token_ttl_days) - timedelta(seconds=1)
        # The idle window would also have elapsed by now, so it is slid
        # forward to isolate the absolute bound as the property under test.
        await repository.update_last_used(issued.session.id, clock.instant)

        assert await service.validate_refresh_token(issued.refresh_token)

    async def test_rejects_an_idle_session_inside_the_absolute_window(
        self, service: SessionService, clock: MovableClock, settings: SessionSettings
    ) -> None:
        """The second guard database.md §4.4 requires. Without it, a
        stolen token stays usable for the full 30 days even though the
        legitimate user stopped using the session weeks earlier."""
        issued = await service.create_session(USER_ID)
        clock.instant = NOW + timedelta(days=settings.idle_timeout_days)

        assert clock.instant < issued.session.expires_at
        with pytest.raises(ExpiredRefreshToken):
            await service.validate_refresh_token(issued.refresh_token)

    async def test_both_expiries_raise_the_same_exception(
        self, service: SessionService, clock: MovableClock, settings: SessionSettings
    ) -> None:
        """Saying *which* window elapsed would disclose when the legitimate
        user last used the session."""
        idle = await service.create_session(USER_ID)
        absolute = await service.create_session(USER_ID)

        clock.instant = NOW + timedelta(days=settings.idle_timeout_days)
        with pytest.raises(ExpiredRefreshToken) as first:
            await service.validate_refresh_token(idle.refresh_token)

        clock.instant = NOW + timedelta(days=settings.refresh_token_ttl_days)
        with pytest.raises(ExpiredRefreshToken) as second:
            await service.validate_refresh_token(absolute.refresh_token)

        assert first.value.message == second.value.message
        assert first.value.code == second.value.code

    async def test_rejects_a_revoked_session(self, service: SessionService) -> None:
        issued = await service.create_session(USER_ID)
        await service.revoke_session(issued.session.id)

        with pytest.raises(RevokedSession):
            await service.validate_refresh_token(issued.refresh_token)

    async def test_every_rejection_shares_one_parent_type(
        self, service: SessionService, clock: MovableClock
    ) -> None:
        """So a refresh endpoint catching `InvalidRefreshToken` cannot let
        one case escape as a 500."""
        revoked = await service.create_session(USER_ID)
        await service.revoke_session(revoked.session.id)
        expired = await service.create_session(USER_ID)

        with pytest.raises(InvalidRefreshToken):
            await service.validate_refresh_token("nonsense")
        with pytest.raises(InvalidRefreshToken):
            await service.validate_refresh_token(revoked.refresh_token)

        clock.instant = NOW + timedelta(days=365)
        with pytest.raises(InvalidRefreshToken):
            await service.validate_refresh_token(expired.refresh_token)


class TestReuseDetection:
    """database.md §14.3's core security property."""

    async def test_presenting_a_revoked_token_revokes_the_whole_family(
        self, service: SessionService, repository: FakeSessionRepository, clock: MovableClock
    ) -> None:
        """The doc's reasoning: "the attacker and the legitimate user now
        both hold links in the same chain, and there is no way to tell
        which one is presenting"."""
        first = await service.create_session(USER_ID)
        family = first.session.token_family

        # A second link in the same chain, as rotation will produce.
        sibling = await _sibling_in_family(service, repository, family, clock)

        await service.revoke_session(first.session.id)
        with pytest.raises(RevokedSession):
            await service.validate_refresh_token(first.refresh_token)

        surviving = await repository.get_by_id(sibling.id)
        assert surviving is not None
        assert surviving.is_revoked is True
        assert surviving.revoked_reason is RevocationReason.REUSE_DETECTED

    async def test_other_families_are_untouched(
        self, service: SessionService, repository: FakeSessionRepository
    ) -> None:
        """Reuse detection kills the compromised chain, not every device
        the player owns. Signing someone out of their phone because their
        laptop's token was replayed is worse than the attack in most
        cases."""
        compromised = await service.create_session(USER_ID)
        other_device = await service.create_session(USER_ID)

        await service.revoke_session(compromised.session.id)
        with pytest.raises(RevokedSession):
            await service.validate_refresh_token(compromised.refresh_token)

        still_live = await repository.get_by_id(other_device.session.id)
        assert still_live is not None
        assert still_live.is_revoked is False

    async def test_other_users_are_untouched(
        self, service: SessionService, repository: FakeSessionRepository
    ) -> None:
        mine = await service.create_session(USER_ID)
        theirs = await service.create_session(OTHER_USER_ID)

        await service.revoke_session(mine.session.id)
        with pytest.raises(RevokedSession):
            await service.validate_refresh_token(mine.refresh_token)

        assert (await repository.get_by_id(theirs.session.id)) is not None
        unaffected = await repository.get_by_id(theirs.session.id)
        assert unaffected is not None and unaffected.is_revoked is False

    async def test_the_original_revocation_reason_is_preserved(
        self, service: SessionService, repository: FakeSessionRepository
    ) -> None:
        """ "First revocation wins" — a `player` sign-out that is later
        replayed keeps its original reason, so the audit trail still says
        the user signed out rather than claiming the platform detected an
        attack."""
        issued = await service.create_session(USER_ID)
        await service.revoke_session(issued.session.id, reason=RevocationReason.PLAYER)

        with pytest.raises(RevokedSession):
            await service.validate_refresh_token(issued.refresh_token)

        stored = await repository.get_by_id(issued.session.id)
        assert stored is not None
        assert stored.revoked_reason is RevocationReason.PLAYER

    async def test_is_logged_at_warning(
        self, service: SessionService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every other refresh rejection is a normal outcome under BE-07.
        This is the only signal the platform has that a token was
        replayed, and its *rate* is what an alert should watch."""
        issued = await service.create_session(USER_ID)
        await service.revoke_session(issued.session.id)

        with caplog.at_level(logging.DEBUG), pytest.raises(RevokedSession):
            await service.validate_refresh_token(issued.refresh_token)

        record = next(r for r in caplog.records if r.message == "refresh_token_reuse_detected")
        assert record.levelno == logging.WARNING

    async def test_does_not_log_the_token(
        self, service: SessionService, caplog: pytest.LogCaptureFixture
    ) -> None:
        issued = await service.create_session(USER_ID)
        await service.revoke_session(issued.session.id)

        with caplog.at_level(logging.DEBUG), pytest.raises(RevokedSession):
            await service.validate_refresh_token(issued.refresh_token)

        assert issued.refresh_token not in caplog.text


class TestRevokeSession:
    async def test_revokes_and_reports_that_it_did(self, service: SessionService) -> None:
        issued = await service.create_session(USER_ID)

        assert await service.revoke_session(issued.session.id) is True

    async def test_is_idempotent(self, service: SessionService) -> None:
        """A caller retrying after a dropped response must not get an
        error for the retry."""
        issued = await service.create_session(USER_ID)
        await service.revoke_session(issued.session.id)

        assert await service.revoke_session(issued.session.id) is False

    async def test_raises_for_a_session_that_never_existed(self, service: SessionService) -> None:
        with pytest.raises(SessionNotFound):
            await service.revoke_session(uuid4())

    async def test_records_the_reason(
        self, service: SessionService, repository: FakeSessionRepository
    ) -> None:
        issued = await service.create_session(USER_ID)

        await service.revoke_session(issued.session.id, reason=RevocationReason.SUSPENSION)

        stored = await repository.get_by_id(issued.session.id)
        assert stored is not None
        assert stored.revoked_reason is RevocationReason.SUSPENSION

    async def test_leaves_other_sessions_alone(
        self, service: SessionService, repository: FakeSessionRepository
    ) -> None:
        target = await service.create_session(USER_ID)
        survivor = await service.create_session(USER_ID)

        await service.revoke_session(target.session.id)

        remaining = await repository.get_by_id(survivor.session.id)
        assert remaining is not None
        assert remaining.is_revoked is False


class TestRevokeAllSessions:
    async def test_revokes_every_live_session(self, service: SessionService) -> None:
        for _ in range(3):
            await service.create_session(USER_ID)

        assert await service.revoke_all_sessions(USER_ID) == 3
        assert await service.list_user_sessions(USER_ID) == []

    async def test_keeps_the_named_session(self, service: SessionService) -> None:
        """SE-1 — "a password change revokes every session except the one
        performing it". Without this, changing your password signs you out
        of the device you changed it on."""
        keep = await service.create_session(USER_ID)
        await service.create_session(USER_ID)
        await service.create_session(USER_ID)

        revoked = await service.revoke_all_sessions(
            USER_ID,
            reason=RevocationReason.PASSWORD_CHANGE,
            except_session_id=keep.session.id,
        )

        assert revoked == 2
        remaining = await service.list_user_sessions(USER_ID)
        assert [session.id for session in remaining] == [keep.session.id]

    async def test_the_kept_session_still_works(self, service: SessionService) -> None:
        keep = await service.create_session(USER_ID)
        await service.create_session(USER_ID)

        await service.revoke_all_sessions(
            USER_ID,
            reason=RevocationReason.PASSWORD_CHANGE,
            except_session_id=keep.session.id,
        )

        assert (await service.validate_refresh_token(keep.refresh_token)).id == keep.session.id

    async def test_does_not_touch_other_users(self, service: SessionService) -> None:
        await service.create_session(USER_ID)
        await service.create_session(OTHER_USER_ID)

        await service.revoke_all_sessions(USER_ID)

        assert len(await service.list_user_sessions(OTHER_USER_ID)) == 1

    async def test_is_idempotent(self, service: SessionService) -> None:
        await service.create_session(USER_ID)
        await service.revoke_all_sessions(USER_ID)

        assert await service.revoke_all_sessions(USER_ID) == 0

    async def test_revoking_none_is_not_an_error(self, service: SessionService) -> None:
        """SE-3 must work for a suspended account that happens to have no
        live session."""
        assert await service.revoke_all_sessions(USER_ID) == 0

    async def test_revoked_sessions_stop_validating(self, service: SessionService) -> None:
        issued = await service.create_session(USER_ID)

        await service.revoke_all_sessions(USER_ID, reason=RevocationReason.SUSPENSION)

        with pytest.raises(RevokedSession):
            await service.validate_refresh_token(issued.refresh_token)


class TestListUserSessions:
    async def test_returns_only_live_sessions_by_default(self, service: SessionService) -> None:
        live = await service.create_session(USER_ID)
        revoked = await service.create_session(USER_ID)
        await service.revoke_session(revoked.session.id)

        listed = await service.list_user_sessions(USER_ID)

        assert [session.id for session in listed] == [live.session.id]

    async def test_can_include_revoked(self, service: SessionService) -> None:
        await service.create_session(USER_ID)
        revoked = await service.create_session(USER_ID)
        await service.revoke_session(revoked.session.id)

        listed = await service.list_user_sessions(USER_ID, include_revoked=True)

        assert len(listed) == 2

    async def test_is_scoped_to_one_user(self, service: SessionService) -> None:
        await service.create_session(USER_ID)
        await service.create_session(OTHER_USER_ID)

        listed = await service.list_user_sessions(USER_ID)

        assert [session.user_id for session in listed] == [USER_ID]

    async def test_is_newest_first(self, service: SessionService, clock: MovableClock) -> None:
        """A device list is read top-down, and the session someone is
        looking for is almost always the one they just created."""
        first = await service.create_session(USER_ID)
        clock.instant = NOW + timedelta(minutes=1)
        second = await service.create_session(USER_ID)

        listed = await service.list_user_sessions(USER_ID)

        assert [session.id for session in listed] == [second.session.id, first.session.id]

    async def test_is_empty_for_a_user_with_no_sessions(self, service: SessionService) -> None:
        assert await service.list_user_sessions(uuid4()) == []


class TestRotationIsPreparedNotImplemented:
    async def test_raises_rather_than_silently_not_rotating(self, service: SessionService) -> None:
        """A64-011.4's brief is "prepare interface only". A method that
        quietly issued a token *without* invalidating the old one would
        look like it worked while disabling reuse detection entirely —
        every old token would stay valid, silently."""
        issued = await service.create_session(USER_ID)

        with pytest.raises(NotImplementedError, match="A64-011.5"):
            await service.rotate_refresh_token(issued.refresh_token)

    async def test_the_interface_a64_011_5_will_call_is_fixed(
        self, service: SessionService
    ) -> None:
        import inspect

        signature = inspect.signature(service.rotate_refresh_token)

        assert set(signature.parameters) == {"refresh_token"}


async def _sibling_in_family(
    service: SessionService,
    repository: FakeSessionRepository,
    family: UUID,
    clock: MovableClock,
) -> UserSession:
    """Adds a second session to an existing family, standing in for what
    rotation will produce once A64-011.5 implements it."""
    sibling = UserSession.start(
        user_id=USER_ID,
        refresh_token_hash=RefreshTokenService(SessionSettings()).hash_refresh_token(
            "sibling-token"
        ),
        issued_at=clock.now(),
        lifetime=timedelta(days=30),
        token_family=family,
    )
    return await repository.create_session(sibling)
