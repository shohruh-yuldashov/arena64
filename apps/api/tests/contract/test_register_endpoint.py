"""Registration against real PostgreSQL 17 and real Argon2id.

`tests/unit/test_register_api.py` covers the HTTP surface with fakes.
This covers what only the real stack can prove:

  - the hash that reaches the database is genuine Argon2id, verifiable
    with the reference library, and is not the plaintext;
  - the `users.user` unique constraints — not the service's pre-check —
    reject a duplicate, and produce the same typed error (BE-06);
  - the whole thing is one transaction that rolls back on failure.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

import pytest
from argon2 import PasswordHasher as Argon2PasswordHasher
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import AuthSettings
from app.core.clock import SystemClock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.auth.application.commands import RegisterUser
from app.modules.auth.application.services import RegistrationService
from app.modules.auth.domain.exceptions import WeakPassword
from app.modules.auth.infrastructure import Argon2idPasswordHasher
from app.modules.users.application.services import UserService
from app.modules.users.application.services.user_account_service import UserAccountService
from app.modules.users.domain.exceptions import EmailAlreadyExists, UsernameAlreadyExists
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository

PASSWORD = "CorrectHorse1!"


@pytest.fixture
def service(contract_session: AsyncSession) -> RegistrationService:
    """The production object graph, with a real session and a real hasher —
    only the session's transaction is the test's (rolled back after)."""
    users = UserService(
        users=SqlAlchemyUserRepository(contract_session),
        unit_of_work=SessionUnitOfWork(contract_session),
        clock=SystemClock(),
    )
    return RegistrationService(
        accounts=UserAccountService(users),
        password_hasher=Argon2idPasswordHasher(AuthSettings()),
    )


def command(
    *, username: str = "player_one", email: str = "player.one@example.com", password: str = PASSWORD
) -> RegisterUser:
    return RegisterUser(
        username=username,
        email=email,
        password=SecretStr(password),
        preferred_language="en",
        timezone="UTC",
    )


class TestWhatReachesTheDatabase:
    async def test_the_stored_value_is_a_real_argon2id_hash(
        self, service: RegistrationService, contract_session: AsyncSession
    ) -> None:
        created = await service.register(command())

        stored = await contract_session.scalar(
            text('SELECT password_hash FROM users."user" WHERE id = :id'),
            {"id": created.id},
        )

        assert stored is not None
        assert stored.startswith("$argon2id$")
        # Verifiable by the reference library — proves registration wrote
        # something A64-011.2's login will actually be able to check.
        assert Argon2PasswordHasher().verify(stored, PASSWORD) is True

    async def test_the_plaintext_is_nowhere_in_the_row(
        self, service: RegistrationService, contract_session: AsyncSession
    ) -> None:
        """Scans the entire row, not just `password_hash` — a password
        accidentally copied into `display_name` or any future column would
        be just as bad, and this is the assertion that would catch it."""
        created = await service.register(command())

        row = await contract_session.execute(
            text('SELECT * FROM users."user" WHERE id = :id'), {"id": created.id}
        )
        rendered = " ".join(str(value) for value in row.mappings().one().values())

        assert PASSWORD not in rendered

    async def test_the_configured_cost_parameters_are_persisted(
        self, service: RegistrationService, contract_session: AsyncSession
    ) -> None:
        """database.md §14.2's rolling-rehash story depends on the stored
        hash carrying the parameters it was made with."""
        settings = AuthSettings()
        created = await service.register(command())

        stored = await contract_session.scalar(
            text('SELECT password_hash FROM users."user" WHERE id = :id'),
            {"id": created.id},
        )

        assert stored is not None
        assert f"m={settings.argon2_memory_cost_kib}" in stored
        assert f"t={settings.argon2_time_cost}" in stored

    async def test_the_account_is_persisted_active_and_unverified(
        self, service: RegistrationService, contract_session: AsyncSession
    ) -> None:
        created = await service.register(command())

        row = await contract_session.execute(
            text('SELECT is_active, is_verified FROM users."user" WHERE id = :id'),
            {"id": created.id},
        )
        is_active, is_verified = row.one()

        assert is_active is True
        assert is_verified is False

    async def test_the_folded_username_is_generated_by_postgres(
        self, service: RegistrationService, contract_session: AsyncSession
    ) -> None:
        created = await service.register(command(username="PlayerOne"))

        folded = await contract_session.scalar(
            text('SELECT username_folded FROM users."user" WHERE id = :id'),
            {"id": created.id},
        )

        assert folded == "playerone"


class TestUniquenessIsEnforcedByTheDatabase:
    async def test_duplicate_username_raises_the_same_typed_error(
        self, service: RegistrationService
    ) -> None:
        await service.register(command(username="taken", email="a@example.com"))

        with pytest.raises(UsernameAlreadyExists):
            await service.register(command(username="taken", email="b@example.com"))

    async def test_duplicate_username_is_rejected_case_insensitively(
        self, service: RegistrationService
    ) -> None:
        # Enforced by `uq_user__username_folded` over the generated column,
        # not by the application — the guard that holds under concurrency.
        await service.register(command(username="Alice", email="a@example.com"))

        with pytest.raises(UsernameAlreadyExists):
            await service.register(command(username="ALICE", email="b@example.com"))

    async def test_duplicate_email_raises_the_same_typed_error(
        self, service: RegistrationService
    ) -> None:
        await service.register(command(username="one", email="dup@example.com"))

        with pytest.raises(EmailAlreadyExists):
            await service.register(command(username="two", email="dup@example.com"))


class TestTransactionality:
    async def test_a_rejected_password_writes_nothing(
        self, service: RegistrationService, contract_session: AsyncSession
    ) -> None:
        with pytest.raises(WeakPassword):
            await service.register(command(password="weak"))

        count = await contract_session.scalar(text('SELECT count(*) FROM users."user"'))
        assert count == 0

    async def test_a_duplicate_leaves_exactly_the_first_account(
        self, service: RegistrationService, contract_session: AsyncSession
    ) -> None:
        await service.register(command(username="first", email="dup@example.com"))

        with pytest.raises(EmailAlreadyExists):
            await service.register(command(username="second", email="dup@example.com"))

        count = await contract_session.scalar(text('SELECT count(*) FROM users."user"'))
        assert count == 1

    async def test_two_distinct_registrations_both_persist(
        self, service: RegistrationService, contract_session: AsyncSession
    ) -> None:
        await service.register(command(username="first", email="first@example.com"))
        await service.register(command(username="second", email="second@example.com"))

        count = await contract_session.scalar(text('SELECT count(*) FROM users."user"'))
        assert count == 2
