"""`AuthenticationService` — orchestration, no database and no real hashing.

The stack under test is production code down to storage: the real
`UserService` and the real `UserCredentialService` behind
`FakeUserRepository`, so email folding, the `UserCredentials` shape and
the compare-and-swap on rehash are all the code that ships. Only the
repository and the hasher are substituted — the latter because real
Argon2id costs ~20ms per call and `test_password_hasher.py` already
proves it works.

`test_authentication_timing.py` covers the one property a stub cannot
show: that an unknown address and a wrong password take the same time.
"""

import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.core.enums import Locale
from app.modules.auth.application.commands import AuthenticateUser
from app.modules.auth.application.services import AuthenticationService
from app.modules.auth.domain.exceptions import (
    AccountLocked,
    InactiveAccount,
    InvalidCredentials,
)
from app.modules.users.application.services import UserService
from app.modules.users.application.services.user_credential_service import UserCredentialService
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import Email, Timezone, Username
from app.modules.users.public import UserCredentials
from tests.fakes.user_repository import FakeUserRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
EMAIL = "player.one@example.com"
PASSWORD = "CorrectHorse1!"
WRONG_PASSWORD = "WrongHorse9?"


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


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
    """A hash is `v{cost}:{plaintext}` — legible, instant, and carrying a
    parameter generation so `needs_rehash` has something real to compare.

    Records every hash it was asked to verify, which is how the tests
    below prove an unknown address still pays for a verification rather
    than short-circuiting.
    """

    #: Not producible by any submitted password (`:` cannot appear before
    #: the first one, and no caller can send a NUL), so verification
    #: against the dummy always fails — exactly like the real one.
    DUMMY_PLAINTEXT = "\x00unguessable"

    def __init__(self, cost: int = 2) -> None:
        self.cost = cost
        self.verified: list[str] = []
        self.hashed: list[str] = []

    async def hash(self, plaintext: str) -> str:
        self.hashed.append(plaintext)
        return f"v{self.cost}:{plaintext}"

    async def verify(self, encoded_hash: str, plaintext: str) -> bool:
        self.verified.append(encoded_hash)
        return encoded_hash.split(":", 1)[-1] == plaintext

    async def needs_rehash(self, encoded_hash: str) -> bool:
        return not encoded_hash.startswith(f"v{self.cost}:")

    async def dummy_hash(self) -> str:
        return f"v{self.cost}:{self.DUMMY_PLAINTEXT}"


class _ExplodingCredentialStore:
    """Wraps a real store and fails only the rehash write, so a test can
    prove a broken security upgrade never costs someone their login."""

    def __init__(self, inner: UserCredentialService) -> None:
        self._inner = inner
        self.attempts = 0

    async def find_credentials_by_email(self, email: str) -> UserCredentials | None:
        return await self._inner.find_credentials_by_email(email)

    async def replace_password_hash(
        self, user_id: UUID, *, expected_hash: str, new_hash: str
    ) -> bool:
        self.attempts += 1
        raise RuntimeError("the database is on fire")


def account(
    *,
    password_hash: str = f"v2:{PASSWORD}",
    is_active: bool = True,
    locked_until: datetime | None = None,
) -> User:
    user = User.create(
        username=Username("player_one"),
        email=Email(EMAIL),
        password_hash=password_hash,
        preferred_language=Locale.EN,
        timezone=Timezone("UTC"),
        created_at=_NOW,
    )
    user.is_active = is_active
    user.locked_until = locked_until
    return user


@pytest.fixture
def repository() -> FakeUserRepository:
    return FakeUserRepository([account()])


@pytest.fixture
def unit_of_work() -> _NullUnitOfWork:
    return _NullUnitOfWork()


@pytest.fixture
def hasher() -> _StubHasher:
    return _StubHasher()


@pytest.fixture
def credentials(
    repository: FakeUserRepository, unit_of_work: _NullUnitOfWork
) -> UserCredentialService:
    users = UserService(users=repository, unit_of_work=unit_of_work, clock=_FixedClock())
    return UserCredentialService(users)


@pytest.fixture
def service(credentials: UserCredentialService, hasher: _StubHasher) -> AuthenticationService:
    return AuthenticationService(
        credentials=credentials, password_hasher=hasher, clock=_FixedClock()
    )


def attempt(*, email: str = EMAIL, password: str = PASSWORD) -> AuthenticateUser:
    return AuthenticateUser(email=email, password=SecretStr(password))


class TestSuccessfulLogin:
    async def test_returns_the_authenticated_account(self, service: AuthenticationService) -> None:
        authenticated = await service.authenticate(attempt())

        assert authenticated.email == EMAIL
        assert authenticated.username == "player_one"

    async def test_never_returns_the_password_hash(self, service: AuthenticationService) -> None:
        """A property of `UserRead`'s shape, not of remembering to strip a
        field — which is why it is asserted against the serialised form as
        well as the object."""
        authenticated = await service.authenticate(attempt())

        assert not hasattr(authenticated, "password_hash")
        assert "password_hash" not in authenticated.model_dump()

    async def test_returns_no_token_of_any_kind(self, service: AuthenticationService) -> None:
        """A64-011.2's boundary. Token issuance is A64-011.3, and a
        placeholder field here would read as "already wired up"."""
        dumped = await service.authenticate(attempt())

        assert not {"token", "access_token", "refresh_token"} & set(dumped.model_dump())

    @pytest.mark.parametrize(
        "submitted",
        [
            pytest.param("PLAYER.ONE@EXAMPLE.COM", id="uppercase"),
            pytest.param("  player.one@example.com  ", id="surrounding-whitespace"),
            pytest.param(" Player.One@Example.com ", id="both"),
        ],
    )
    async def test_the_address_is_normalised_before_lookup(
        self, service: AuthenticationService, submitted: str
    ) -> None:
        """AC-1's case-insensitive addresses, from the caller's side: a
        phone keyboard capitalises the first letter, and someone who
        registered as "player.one@…" must still be able to sign in."""
        assert (await service.authenticate(attempt(email=submitted))).email == EMAIL


class TestFailedLogin:
    async def test_wrong_password_raises_invalid_credentials(
        self, service: AuthenticationService
    ) -> None:
        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(password=WRONG_PASSWORD))

    async def test_unknown_email_raises_invalid_credentials(
        self, service: AuthenticationService
    ) -> None:
        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(email="nobody@example.com"))

    async def test_both_failures_are_indistinguishable(
        self, service: AuthenticationService
    ) -> None:
        """The account-enumeration guard, asserted on everything a client
        can actually observe in the response: the type, the wire code and
        the message. Elapsed time is the fourth observable, and
        `test_authentication_timing.py` covers it.
        """
        with pytest.raises(InvalidCredentials) as unknown:
            await service.authenticate(attempt(email="nobody@example.com"))
        with pytest.raises(InvalidCredentials) as wrong:
            await service.authenticate(attempt(password=WRONG_PASSWORD))

        assert unknown.value.code == wrong.value.code
        assert unknown.value.message == wrong.value.message

    async def test_the_message_never_echoes_the_submitted_address(
        self, service: AuthenticationService
    ) -> None:
        """An error string is a place personal data reaches logs and
        screenshots (services.md §8.5) — and here it would also prove the
        server read the value, undoing the ambiguity above."""
        with pytest.raises(InvalidCredentials) as raised:
            await service.authenticate(attempt(password=WRONG_PASSWORD))

        assert EMAIL not in raised.value.message

    async def test_the_message_never_contains_the_submitted_password(
        self, service: AuthenticationService
    ) -> None:
        with pytest.raises(InvalidCredentials) as raised:
            await service.authenticate(attempt(password=WRONG_PASSWORD))

        assert WRONG_PASSWORD not in raised.value.message

    async def test_an_unknown_address_still_costs_a_verification(
        self, service: AuthenticationService, hasher: _StubHasher
    ) -> None:
        """The mechanism behind the timing test, asserted structurally so
        that a regression is caught in microseconds rather than by a
        statistical bound: an early `return` for the unknown-address branch
        would leave this list empty.
        """
        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(email="nobody@example.com"))

        assert hasher.verified == [await hasher.dummy_hash()]

    async def test_a_known_address_costs_exactly_one_verification(
        self, service: AuthenticationService, hasher: _StubHasher
    ) -> None:
        """One, matching the unknown-address path exactly. Two would be as
        much of a timing signal as none."""
        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(password=WRONG_PASSWORD))

        assert len(hasher.verified) == 1

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param("not-an-email", id="no-at-sign"),
            pytest.param("@example.com", id="no-local-part"),
            pytest.param("", id="empty"),
        ],
    )
    async def test_a_malformed_address_fails_as_invalid_credentials(
        self, service: AuthenticationService, malformed: str
    ) -> None:
        """Not `InvalidEmail`. The HTTP schema rejects malformed addresses
        earlier with a field-level 422, which is the right feedback for a
        form; every other caller must not be able to tell "not a valid
        address" from "wrong password"."""
        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(email=malformed))


class TestInactiveAccount:
    @pytest.fixture
    def repository(self) -> FakeUserRepository:
        return FakeUserRepository([account(is_active=False)])

    async def test_correct_password_raises_inactive_account(
        self, service: AuthenticationService
    ) -> None:
        with pytest.raises(InactiveAccount):
            await service.authenticate(attempt())

    async def test_a_wrong_password_still_looks_like_invalid_credentials(
        self, service: AuthenticationService
    ) -> None:
        """The order that makes `InactiveAccount` safe to expose at all:
        the status check happens *after* verification, so someone who does
        not know the password cannot use this endpoint to discover that an
        address exists but is disabled."""
        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(password=WRONG_PASSWORD))

    async def test_an_unknown_address_is_unaffected(self, service: AuthenticationService) -> None:
        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(email="nobody@example.com"))


class TestLockedAccount:
    @pytest.fixture
    def repository(self) -> FakeUserRepository:
        return FakeUserRepository([account(locked_until=_NOW + timedelta(minutes=15))])

    async def test_correct_password_raises_account_locked(
        self, service: AuthenticationService
    ) -> None:
        with pytest.raises(AccountLocked):
            await service.authenticate(attempt())

    async def test_a_wrong_password_still_looks_like_invalid_credentials(
        self, service: AuthenticationService
    ) -> None:
        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(password=WRONG_PASSWORD))


class TestLapsedLock:
    @pytest.fixture
    def repository(self) -> FakeUserRepository:
        return FakeUserRepository([account(locked_until=_NOW - timedelta(seconds=1))])

    async def test_a_lock_in_the_past_no_longer_bars_sign_in(
        self, service: AuthenticationService
    ) -> None:
        """Expiry is evaluated on read against the injected clock, so a
        lock lapses by itself — nothing has to run to clear it, and a job
        that failed could not leave someone locked out."""
        assert (await service.authenticate(attempt())).email == EMAIL


class TestRehashOnLogin:
    @pytest.fixture
    def repository(self) -> FakeUserRepository:
        # Hashed under the *previous* generation of parameters.
        return FakeUserRepository([account(password_hash=f"v1:{PASSWORD}")])

    async def test_a_stale_hash_is_upgraded(
        self, service: AuthenticationService, repository: FakeUserRepository
    ) -> None:
        """database.md §14.2. Without this, raising the configured cost
        would apply to new accounts only, and every account created before
        the raise would stay at the old parameters for life."""
        authenticated = await service.authenticate(attempt())
        stored = await repository.get_by_id(authenticated.id)

        assert stored is not None
        assert stored.password_hash == f"v2:{PASSWORD}"

    async def test_the_upgraded_hash_still_authenticates(
        self, service: AuthenticationService
    ) -> None:
        """The rehash must encode the *same* password. Re-running the whole
        sign-in against the rewritten row is the only assertion that
        actually proves it."""
        await service.authenticate(attempt())

        assert (await service.authenticate(attempt())).email == EMAIL

    async def test_a_current_hash_is_left_alone(
        self, credentials: UserCredentialService, hasher: _StubHasher
    ) -> None:
        """No write, and — more to the point — no second Argon2 hash on the
        overwhelmingly common path where nothing needs upgrading."""
        repository = FakeUserRepository([account()])
        users = UserService(users=repository, unit_of_work=_NullUnitOfWork(), clock=_FixedClock())
        service = AuthenticationService(
            credentials=UserCredentialService(users),
            password_hasher=hasher,
            clock=_FixedClock(),
        )

        await service.authenticate(attempt())

        assert hasher.hashed == []

    async def test_a_failed_rehash_never_fails_the_login(
        self, credentials: UserCredentialService, hasher: _StubHasher
    ) -> None:
        """Fail-open, deliberately: the caller has already proved who they
        are, and refusing a valid sign-in because an opportunistic security
        upgrade could not be written would turn a background improvement
        into an outage."""
        store = _ExplodingCredentialStore(credentials)
        service = AuthenticationService(
            credentials=store, password_hasher=hasher, clock=_FixedClock()
        )

        assert (await service.authenticate(attempt())).email == EMAIL
        assert store.attempts == 1

    async def test_a_failed_rehash_is_logged_as_a_warning(
        self,
        credentials: UserCredentialService,
        hasher: _StubHasher,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Swallowed for the request, not swallowed for operators: a rehash
        that never succeeds is a real problem, just not this caller's."""
        service = AuthenticationService(
            credentials=_ExplodingCredentialStore(credentials),
            password_hasher=hasher,
            clock=_FixedClock(),
        )

        with caplog.at_level(logging.WARNING):
            await service.authenticate(attempt())

        assert "password_rehash_failed" in caplog.text


class TestLogging:
    async def test_a_successful_login_is_logged_with_the_user_id(
        self, service: AuthenticationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            authenticated = await service.authenticate(attempt())

        record = next(r for r in caplog.records if r.message == "login_succeeded")
        assert record.user_id == str(authenticated.id)  # type: ignore[attr-defined]

    async def test_a_failed_login_is_logged(
        self, service: AuthenticationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO), pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(password=WRONG_PASSWORD))

        assert any(record.message == "login_failed" for record in caplog.records)

    async def test_a_failed_login_logs_at_info_not_error(
        self, service: AuthenticationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """BE-07: a domain error is a normal outcome and is never logged as
        an error. A wrong password at ERROR would page somebody every time
        a human mistyped — and would hide the real signal, which is the
        *rate* of these, not any one of them."""
        with caplog.at_level(logging.DEBUG), pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(password=WRONG_PASSWORD))

        failed = next(r for r in caplog.records if r.message == "login_failed")
        assert failed.levelno == logging.INFO

    @pytest.mark.parametrize(
        "scenario",
        [
            pytest.param({"password": WRONG_PASSWORD}, id="wrong-password"),
            pytest.param({"email": "nobody@example.com"}, id="unknown-email"),
            pytest.param({}, id="success"),
        ],
    )
    async def test_nothing_ever_logs_a_password(
        self,
        service: AuthenticationService,
        caplog: pytest.LogCaptureFixture,
        scenario: dict[str, str],
    ) -> None:
        with caplog.at_level(logging.DEBUG), suppress(InvalidCredentials):
            await service.authenticate(attempt(**scenario))

        assert PASSWORD not in caplog.text
        assert WRONG_PASSWORD not in caplog.text

    async def test_a_failed_login_does_not_log_the_submitted_address(
        self, service: AuthenticationService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No user id is resolvable for an unknown address, and the address
        itself is personal data — a log line is a permanent record in a
        system with broader read access than the database (services.md
        §8.5)."""
        with caplog.at_level(logging.DEBUG), pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(email="somebody@example.com"))

        assert "somebody@example.com" not in caplog.text
