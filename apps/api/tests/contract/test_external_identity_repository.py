"""`users.external_identity`, against real PostgreSQL — A64-031.E1.

What only the database can show: that an account and its Google link
commit or roll back **together**, that the two unique constraints hold
when two first sign-ins race on separate connections, and that a link
leaves with its account. `tests/fakes/external_identity_repository.py`
reproduces the constraints for the unit suite; it cannot reproduce any of
this.

Skipped, not failed, when PostgreSQL is unreachable.
"""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import SystemClock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.users.application.commands import CreateUser
from app.modules.users.application.services import UserService
from app.modules.users.domain.exceptions import (
    EmailAlreadyExists,
    ExternalIdentityAlreadyLinked,
    UsernameAlreadyExists,
)
from app.modules.users.domain.external_identity import ExternalIdentity, IdentityProvider
from app.modules.users.infrastructure.models import UserExternalIdentityModel, UserModel
from app.modules.users.infrastructure.repositories import (
    SqlAlchemyExternalIdentityRepository,
    SqlAlchemyUserRepository,
)

GOOGLE = IdentityProvider.GOOGLE


def _service(session: AsyncSession) -> UserService:
    return UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=SystemClock(),
        external_identities=SqlAlchemyExternalIdentityRepository(session),
    )


def _command(username: str, email: str) -> CreateUser:
    return CreateUser(
        username=username,
        email=email,
        password_hash="$argon2id$unusable",
        preferred_language="en",
        timezone="UTC",
    )


async def _count(session: AsyncSession, model: type[UserModel | UserExternalIdentityModel]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


class TestCreatingWithAnIdentity:
    async def test_the_account_is_verified_and_linked(self, contract_session: AsyncSession) -> None:
        users = _service(contract_session)

        created = await users.create_user_with_external_identity(
            _command("google_one", "google.one@gmail.com"), ExternalIdentity(GOOGLE, "sub-1")
        )

        found = await users.lookup_by_external_identity(ExternalIdentity(GOOGLE, "sub-1"))
        assert created.is_verified is True
        assert found is not None and found.id == created.id

    async def test_an_identity_already_linked_creates_no_account(
        self, contract_session: AsyncSession
    ) -> None:
        """Atomicity: the link fails on `uq_external_identity__provider_subject`
        and the user row written before it in the same transaction goes too."""
        users = _service(contract_session)
        await users.create_user_with_external_identity(
            _command("first_owner", "first@gmail.com"), ExternalIdentity(GOOGLE, "sub-2")
        )

        with pytest.raises(ExternalIdentityAlreadyLinked):
            await users.create_user_with_external_identity(
                _command("second_owner", "second@gmail.com"), ExternalIdentity(GOOGLE, "sub-2")
            )

        assert await users.lookup_by_email("second@gmail.com") is None
        assert await _count(contract_session, UserModel) == 1

    async def test_the_username_rules_still_apply(self, contract_session: AsyncSession) -> None:
        users = _service(contract_session)
        await users.create_user(_command("Taken_Name", "someone@example.com"))

        with pytest.raises(UsernameAlreadyExists):
            await users.create_user_with_external_identity(
                _command("taken_name", "other@gmail.com"), ExternalIdentity(GOOGLE, "sub-3")
            )

        assert await _count(contract_session, UserExternalIdentityModel) == 0


class TestLinking:
    async def test_links_an_existing_account(self, contract_session: AsyncSession) -> None:
        users = _service(contract_session)
        existing = await users.create_user(_command("has_password", "has.password@gmail.com"))

        await users.link_external_identity(existing.id, ExternalIdentity(GOOGLE, "sub-4"))

        found = await users.lookup_by_external_identity(ExternalIdentity(GOOGLE, "sub-4"))
        assert found is not None and found.id == existing.id

    async def test_an_account_holds_one_google_identity(
        self, contract_session: AsyncSession
    ) -> None:
        users = _service(contract_session)
        existing = await users.create_user(_command("one_google", "one.google@gmail.com"))
        await users.link_external_identity(existing.id, ExternalIdentity(GOOGLE, "sub-5"))

        with pytest.raises(ExternalIdentityAlreadyLinked):
            await users.link_external_identity(existing.id, ExternalIdentity(GOOGLE, "sub-6"))

    async def test_an_identity_reaches_one_account(self, contract_session: AsyncSession) -> None:
        users = _service(contract_session)
        first = await users.create_user(_command("first_acct", "first.acct@gmail.com"))
        second = await users.create_user(_command("second_acct", "second.acct@gmail.com"))
        await users.link_external_identity(first.id, ExternalIdentity(GOOGLE, "sub-7"))

        with pytest.raises(ExternalIdentityAlreadyLinked):
            await users.link_external_identity(second.id, ExternalIdentity(GOOGLE, "sub-7"))

    async def test_a_link_leaves_with_its_account(self, contract_session: AsyncSession) -> None:
        users = _service(contract_session)
        created = await users.create_user_with_external_identity(
            _command("leaving_soon", "leaving@gmail.com"), ExternalIdentity(GOOGLE, "sub-8")
        )

        await contract_session.execute(
            text('DELETE FROM users."user" WHERE id = :id'), {"id": created.id}
        )

        assert await _count(contract_session, UserExternalIdentityModel) == 0


class TestTwoFirstSignInsAtOnce:
    """Two callbacks for one Google account's first sign-in, on two
    replicas: separate connections, committing for real. Exactly one
    account and one link may survive, whichever constraint stops the other.
    """

    @pytest.mark.parametrize(
        "emails",
        [
            pytest.param(("same@gmail.com", "same@gmail.com"), id="same address"),
            pytest.param(("before@gmail.com", "after@gmail.com"), id="address changed between"),
        ],
    )
    async def test_exactly_one_account_is_created(
        self, contract_engine: AsyncEngine, emails: tuple[str, str]
    ) -> None:
        identity = ExternalIdentity(GOOGLE, "racing-sub")

        async def first_sign_in(index: int) -> UUID | Exception:
            async with AsyncSession(contract_engine, expire_on_commit=False) as session:
                try:
                    created = await _service(session).create_user_with_external_identity(
                        _command(f"racer_{index}", emails[index]), identity
                    )
                except (EmailAlreadyExists, ExternalIdentityAlreadyLinked) as lost:
                    return lost
                return created.id

        outcomes = await asyncio.gather(first_sign_in(0), first_sign_in(1))

        winners = [outcome for outcome in outcomes if isinstance(outcome, UUID)]
        async with AsyncSession(contract_engine) as session:
            accounts = await _count(session, UserModel)
            links = list(await session.scalars(select(UserExternalIdentityModel.user_id)))
        assert len(winners) == 1
        assert accounts == 1
        assert links == winners
