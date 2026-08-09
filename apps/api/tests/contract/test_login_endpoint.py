"""Login against real PostgreSQL 17 and real Argon2id.

`tests/unit/test_login_api.py` covers the HTTP surface with fakes and
`tests/unit/test_authentication_service.py` covers the orchestration.
This covers what only the real stack can prove:

  - a password registered through the production path verifies through
    the production login path — the two halves were written against a
    stub each, and nothing else checks they agree;
  - the rehash-on-login `UPDATE` actually lands in `users.user`, and its
    compare-and-swap declines when the row moved;
  - `locked_until` round-trips through the column added by
    `31528456f438` as a timezone-aware instant.

Registration is used to create the fixtures rather than inserting rows by
hand, deliberately: an account built by any other route would not prove
that what registration writes is what login can read.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

import statistics
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import AuthSettings
from app.core.clock import SystemClock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.auth.application.commands import AuthenticateUser, RegisterUser
from app.modules.auth.application.services import AuthenticationService, RegistrationService
from app.modules.auth.domain.exceptions import (
    AccountLocked,
    InactiveAccount,
    InvalidCredentials,
)
from app.modules.auth.infrastructure import Argon2idPasswordHasher
from app.modules.users.application.services import UserService
from app.modules.users.application.services.user_account_service import UserAccountService
from app.modules.users.application.services.user_credential_service import UserCredentialService
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from tests.fakes.moderation import UnrestrictedAccounts

EMAIL = "player.one@example.com"
PASSWORD = "CorrectHorse1!"
WRONG_PASSWORD = "WrongHorse9?"

#: Deliberately weaker than `AuthSettings()`'s defaults, so a hash made
#: with it reports `needs_rehash` against the current configuration —
#: which is how the rehash-on-login tests below reach that branch without
#: mutating global settings.
LEGACY_PARAMETERS = AuthSettings(argon2_time_cost=1, argon2_memory_cost_kib=8192)


def _users(session: AsyncSession) -> UserService:
    return UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=SystemClock(),
    )


@pytest.fixture
def registration(contract_session: AsyncSession) -> RegistrationService:
    """Registers at the *legacy* parameters, so every account these tests
    create is one the current configuration wants to upgrade."""
    return RegistrationService(
        accounts=UserAccountService(_users(contract_session)),
        password_hasher=Argon2idPasswordHasher(LEGACY_PARAMETERS),
    )


@pytest.fixture
def service(contract_session: AsyncSession) -> AuthenticationService:
    """The production object graph, with a real session and a real hasher
    — only the session's transaction is the test's (rolled back after)."""
    return AuthenticationService(
        restrictions=UnrestrictedAccounts(),
        credentials=UserCredentialService(_users(contract_session)),
        password_hasher=Argon2idPasswordHasher(AuthSettings()),
        clock=SystemClock(),
    )


@pytest.fixture
async def registered(registration: RegistrationService) -> str:
    created = await registration.register(
        RegisterUser(
            username="player_one",
            email=EMAIL,
            password=SecretStr(PASSWORD),
            preferred_language="en",
            timezone="UTC",
        )
    )
    return str(created.id)


def attempt(*, email: str = EMAIL, password: str = PASSWORD) -> AuthenticateUser:
    return AuthenticateUser(email=email, password=SecretStr(password))


class TestAgainstWhatRegistrationActuallyWrote:
    async def test_a_registered_password_authenticates(
        self, service: AuthenticationService, registered: str
    ) -> None:
        """The end-to-end assertion neither half's own tests can make:
        registration hashes behind a port and login verifies behind the
        same port, each tested against a stub. This is what proves the two
        production implementations agree.
        """
        assert str((await service.authenticate(attempt())).id) == registered

    async def test_a_wrong_password_is_rejected(
        self, service: AuthenticationService, registered: str
    ) -> None:
        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(password=WRONG_PASSWORD))

    async def test_an_unregistered_address_is_rejected_identically(
        self, service: AuthenticationService, registered: str
    ) -> None:
        with pytest.raises(InvalidCredentials) as unknown:
            await service.authenticate(attempt(email="nobody@example.com"))
        with pytest.raises(InvalidCredentials) as wrong:
            await service.authenticate(attempt(password=WRONG_PASSWORD))

        assert unknown.value.code == wrong.value.code
        assert unknown.value.message == wrong.value.message

    async def test_the_address_matches_case_insensitively_in_postgres(
        self, service: AuthenticationService, registered: str
    ) -> None:
        """AC-1, enforced by the query rather than by Python: the folding
        that registration applied and the folding this lookup applies must
        be the same, and only real PostgreSQL can show that they are."""
        assert str((await service.authenticate(attempt(email=EMAIL.upper()))).id) == registered

    async def test_the_returned_account_carries_no_hash(
        self, service: AuthenticationService, registered: str
    ) -> None:
        authenticated = await service.authenticate(attempt())

        assert "password_hash" not in authenticated.model_dump()
        assert PASSWORD not in str(authenticated.model_dump())


class TestRehashOnLogin:
    async def test_the_stored_hash_is_upgraded_in_place(
        self,
        service: AuthenticationService,
        contract_session: AsyncSession,
        registered: str,
    ) -> None:
        """database.md §14.2, all the way to the row.

        The account was registered at `LEGACY_PARAMETERS`; the login
        service runs at the current ones, so a successful sign-in must
        rewrite the column.
        """
        before = await _stored_hash(contract_session, registered)
        assert f"m={LEGACY_PARAMETERS.argon2_memory_cost_kib}" in before

        await service.authenticate(attempt())

        after = await _stored_hash(contract_session, registered)
        assert f"m={AuthSettings().argon2_memory_cost_kib}" in after
        assert after != before

    async def test_the_upgraded_hash_authenticates_the_same_password(
        self, service: AuthenticationService, registered: str
    ) -> None:
        """The property that makes the upgrade safe rather than a lockout:
        the rewritten hash must encode the *same* password. Signing in
        twice is the only assertion that actually proves it."""
        await service.authenticate(attempt())

        assert str((await service.authenticate(attempt())).id) == registered

    async def test_a_second_login_does_not_rewrite_an_already_current_hash(
        self,
        service: AuthenticationService,
        contract_session: AsyncSession,
        registered: str,
    ) -> None:
        """Otherwise every sign-in on the platform would carry an extra
        Argon2 hash and an extra `UPDATE` forever."""
        await service.authenticate(attempt())
        upgraded = await _stored_hash(contract_session, registered)

        await service.authenticate(attempt())

        assert await _stored_hash(contract_session, registered) == upgraded

    async def test_a_failed_rehash_still_lets_the_login_succeed(
        self,
        service: AuthenticationService,
        contract_session: AsyncSession,
        registered: str,
    ) -> None:
        """Fail-open. Simulated the only way that reaches the real
        compare-and-swap: change the row underneath the read, so the
        `WHERE password_hash = :expected` matches nothing.

        The service reads credentials first and writes afterwards, so a
        concurrent password change lands in that window — and when it
        does, the login it interleaved with must still succeed rather than
        failing on a security upgrade that was only ever opportunistic.
        """
        credentials = UserCredentialService(_users(contract_session))
        stored = await credentials.find_credentials_by_email(EMAIL)
        assert stored is not None

        applied = await credentials.replace_password_hash(
            stored.account.id,
            expected_hash=stored.password_hash,
            new_hash=await Argon2idPasswordHasher(AuthSettings()).hash(PASSWORD),
        )
        assert applied is True

        # The rehash inside this call computes its CAS against a hash that
        # is now stale, so the write declines — and the login is unmoved.
        assert str((await service.authenticate(attempt())).id) == registered

    async def test_the_compare_and_swap_declines_a_stale_expectation(
        self, contract_session: AsyncSession, registered: str
    ) -> None:
        credentials = UserCredentialService(_users(contract_session))

        applied = await credentials.replace_password_hash(
            _uuid(registered),
            expected_hash="$argon2id$never-what-is-stored",
            new_hash="$argon2id$replacement",
        )

        assert applied is False
        assert "never-what-is-stored" not in await _stored_hash(contract_session, registered)


class TestTimingAgainstRealStorage:
    """The enumeration guard where `tests/unit/test_authentication_timing.py`
    cannot reach.

    That module runs against `FakeUserRepository`, so a lookup that hits
    and a lookup that misses cost the same there by construction — which
    made it blind to the real leak found while smoke-testing A64-011.2: a
    *hit* cost 11.5ms more than a *miss*, not in the query (0.40ms against
    0.35ms) but in `_to_domain`, where constructing a `Timezone` rebuilt
    the entire IANA name set. The two Argon2 verifications agreed to
    within 1%; this disagreed by 33× and swamped them.

    A fake cannot show that, so this asserts it against the adapter that
    can. See `users.domain.validators._known_timezones` for the fix.
    """

    SAMPLES = 5

    @pytest.fixture
    def registration(self, contract_session: AsyncSession) -> RegistrationService:
        """Overrides the module fixture to register at the **current**
        parameters rather than the legacy ones.

        Necessary, and the reason is itself worth recording: a hash made at
        weaker parameters verifies *faster* than the dummy, which is made
        at the current ones. Left on the legacy fixture this test measured
        3.5ms against 15.7ms and failed in the opposite direction. That is
        a real residual signal — an account still on old parameters is
        cheaper to check than one that does not exist — but it is bounded
        (it appears only between a parameter raise and that account's next
        successful sign-in, which rehashes it) and closing it would mean
        padding every login to the slowest possible verification. The
        property under test here is the one that applies to every account
        in steady state.
        """
        return RegistrationService(
            accounts=UserAccountService(_users(contract_session)),
            password_hasher=Argon2idPasswordHasher(AuthSettings()),
        )

    async def _median_ms(self, service: AuthenticationService, *, email: str) -> float:
        samples: list[float] = []
        for _ in range(self.SAMPLES):
            started = time.perf_counter()
            with pytest.raises(InvalidCredentials):
                await service.authenticate(attempt(email=email, password=WRONG_PASSWORD))
            samples.append((time.perf_counter() - started) * 1000)
        return statistics.median(samples)

    async def test_a_registered_address_costs_the_same_as_an_unknown_one(
        self, service: AuthenticationService, registered: str
    ) -> None:
        # One of each first: the memoised dummy hash and the memoised
        # timezone set are both per-process, and measuring their one-time
        # construction would measure the wrong thing.
        await self._median_ms(service, email=EMAIL)
        await self._median_ms(service, email="nobody@example.com")

        known = await self._median_ms(service, email=EMAIL)
        unknown = await self._median_ms(service, email="nobody@example.com")

        difference = abs(known - unknown) / known
        assert difference < 0.35, (
            f"account enumeration by timing against real storage: a registered "
            f"address took {known:.1f}ms against {unknown:.1f}ms for an unknown "
            f"one ({difference:.0%} apart). Something on the row-mapping path "
            f"costs materially more for a hit than for a miss."
        )


class TestAccountState:
    async def test_a_deactivated_account_is_refused_after_verification(
        self, service: AuthenticationService, contract_session: AsyncSession, registered: str
    ) -> None:
        await _set(contract_session, registered, "is_active = false")

        with pytest.raises(InactiveAccount):
            await service.authenticate(attempt())

    async def test_a_deactivated_account_with_a_wrong_password_is_a_generic_failure(
        self, service: AuthenticationService, contract_session: AsyncSession, registered: str
    ) -> None:
        await _set(contract_session, registered, "is_active = false")

        with pytest.raises(InvalidCredentials):
            await service.authenticate(attempt(password=WRONG_PASSWORD))

    async def test_a_future_lock_bars_sign_in(
        self, service: AuthenticationService, contract_session: AsyncSession, registered: str
    ) -> None:
        await _lock_until(contract_session, registered, datetime.now(UTC) + timedelta(hours=1))

        with pytest.raises(AccountLocked):
            await service.authenticate(attempt())

    async def test_a_lapsed_lock_does_not(
        self, service: AuthenticationService, contract_session: AsyncSession, registered: str
    ) -> None:
        """Expiry is evaluated on read, so the lock lifts itself — nothing
        has to run, and a sweeper that failed could not strand anyone."""
        await _lock_until(contract_session, registered, datetime.now(UTC) - timedelta(seconds=1))

        assert str((await service.authenticate(attempt())).id) == registered

    async def test_locked_until_round_trips_as_an_aware_instant(
        self, contract_session: AsyncSession, registered: str
    ) -> None:
        """`UtcDateTime` must hand back something the service can compare
        against an aware `now()`. A naive value here would raise
        `TypeError` on the comparison instead of locking anyone."""
        expires = datetime.now(UTC) + timedelta(hours=1)
        await _lock_until(contract_session, registered, expires)

        stored = await UserCredentialService(_users(contract_session)).find_credentials_by_email(
            EMAIL
        )

        assert stored is not None
        assert stored.locked_until is not None
        assert stored.locked_until.tzinfo is not None
        assert stored.locked_until == expires


async def _stored_hash(session: AsyncSession, user_id: str) -> str:
    stored = await session.scalar(
        text('SELECT password_hash FROM users."user" WHERE id = :id'), {"id": _uuid(user_id)}
    )
    assert stored is not None
    return str(stored)


async def _set(session: AsyncSession, user_id: str, assignment: str) -> None:
    await session.execute(
        text(f'UPDATE users."user" SET {assignment} WHERE id = :id'), {"id": _uuid(user_id)}
    )


async def _lock_until(session: AsyncSession, user_id: str, instant: datetime) -> None:
    await session.execute(
        text('UPDATE users."user" SET locked_until = :until WHERE id = :id'),
        {"id": _uuid(user_id), "until": instant},
    )


def _uuid(value: str) -> UUID:
    return UUID(value)
