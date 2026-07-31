"""The `UserRepository` contract — repositories.md RP-05.

**One suite, two implementations.** Every test below runs twice: once
against `FakeUserRepository` (in-memory) and once against
`SqlAlchemyUserRepository` (real PostgreSQL 17). That is the entire point
of RP-05 — "a fake that has quietly diverged from its real adapter
produces the worst possible test outcome: a green suite over broken
behaviour." Running one suite against both means the fake is *proven*
equivalent on every property these tests express, so the service tests
built on it are trustworthy.

The real-adapter half is skipped, not failed, when PostgreSQL is
unreachable (see `conftest.py`), so this suite still runs for a
contributor without Docker — with the fake half covering what it can.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Locale
from app.core.pagination import CursorPageParams
from app.modules.users.application.ports import UserRepository
from app.modules.users.domain.entities import User
from app.modules.users.domain.exceptions import EmailAlreadyExists, UsernameAlreadyExists
from app.modules.users.domain.value_objects import Email, Timezone, Username
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from tests.fakes.user_repository import FakeUserRepository

_BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_user(
    *,
    username: str = "player_one",
    email: str = "player.one@example.com",
    created_at: datetime | None = None,
) -> User:
    """A valid user with everything but the field under test defaulted.

    Always active and unverified — the states `User.create` produces. A
    test that needs otherwise calls `.deactivate()` on the result, which
    also exercises the entity method rather than constructing the state
    behind its back.
    """
    return User.create(
        username=Username(username),
        email=Email(email),
        password_hash="argon2id$fake$notarealhash",
        preferred_language=Locale.EN,
        timezone=Timezone("UTC"),
        created_at=created_at or _BASE_TIME,
    )


# --- the two implementations under test -------------------------------------


@pytest_asyncio.fixture
async def fake_repository() -> AsyncIterator[UserRepository]:
    yield FakeUserRepository()


@pytest_asyncio.fixture
async def sqlalchemy_repository(contract_session: AsyncSession) -> AsyncIterator[UserRepository]:
    yield SqlAlchemyUserRepository(contract_session)


@pytest.fixture(params=["fake", "sqlalchemy"])
def repository(request: pytest.FixtureRequest) -> UserRepository:
    """Parametrised over both adapters — this is what makes every test in
    this file run twice. `getfixturevalue` is used rather than two copies
    of each test so there is exactly one place the contract is stated."""
    fixture_name = f"{request.param}_repository"
    return request.getfixturevalue(fixture_name)  # type: ignore[no-any-return]


# --- create -----------------------------------------------------------------


class TestCreate:
    async def test_persists_and_returns_the_user(self, repository: UserRepository) -> None:
        user = make_user()
        created = await repository.create(user)

        assert created.id == user.id
        assert created.username.value == "player_one"

    async def test_a_created_user_is_retrievable_by_id(self, repository: UserRepository) -> None:
        user = await repository.create(make_user())
        found = await repository.get_by_id(user.id)

        assert found is not None
        assert found.id == user.id

    async def test_duplicate_username_raises(self, repository: UserRepository) -> None:
        await repository.create(make_user(username="taken", email="a@example.com"))

        with pytest.raises(UsernameAlreadyExists):
            await repository.create(make_user(username="taken", email="b@example.com"))

    async def test_duplicate_username_is_case_insensitive(self, repository: UserRepository) -> None:
        # UP-1: the whole reason `username_folded` exists. If this passed
        # for the fake and failed for PostgreSQL (or the reverse), the
        # service's uniqueness pre-check and the constraint would disagree.
        await repository.create(make_user(username="Alice", email="a@example.com"))

        with pytest.raises(UsernameAlreadyExists):
            await repository.create(make_user(username="ALICE", email="b@example.com"))

    async def test_duplicate_email_raises(self, repository: UserRepository) -> None:
        await repository.create(make_user(username="one", email="dup@example.com"))

        with pytest.raises(EmailAlreadyExists):
            await repository.create(make_user(username="two", email="dup@example.com"))

    async def test_duplicate_email_is_case_insensitive(self, repository: UserRepository) -> None:
        # AC-1. The `Email` value object normalises on construction, so
        # both sides store the same string and a plain unique index
        # suffices — this proves that normalisation actually happens.
        await repository.create(make_user(username="one", email="Dup@Example.COM"))

        with pytest.raises(EmailAlreadyExists):
            await repository.create(make_user(username="two", email="dup@example.com"))


# --- reads ------------------------------------------------------------------


class TestGetById:
    async def test_returns_none_when_absent(self, repository: UserRepository) -> None:
        assert await repository.get_by_id(uuid4()) is None

    async def test_round_trips_every_field(self, repository: UserRepository) -> None:
        user = User.create(
            username=Username("full_fields"),
            email=Email("full@example.com"),
            password_hash="argon2id$fake$hash",
            preferred_language=Locale.RU,
            timezone=Timezone("Asia/Samarkand"),
            created_at=_BASE_TIME,
            display_name="Full Fields",
            avatar_url="https://cdn.example.com/a.png",
        )
        await repository.create(user)

        found = await repository.get_by_id(user.id)
        assert found is not None
        assert found.username.value == "full_fields"
        assert found.email.value == "full@example.com"
        assert found.password_hash == "argon2id$fake$hash"
        assert found.preferred_language is Locale.RU
        assert found.timezone.value == "Asia/Samarkand"
        assert found.display_name == "Full Fields"
        assert found.avatar_url == "https://cdn.example.com/a.png"
        assert found.is_active is True
        assert found.is_verified is False
        assert found.created_at == _BASE_TIME


class TestGetByUsername:
    async def test_returns_none_when_absent(self, repository: UserRepository) -> None:
        assert await repository.get_by_username(Username("nobody_here")) is None

    async def test_finds_regardless_of_case(self, repository: UserRepository) -> None:
        await repository.create(make_user(username="MixedCase"))

        found = await repository.get_by_username(Username("mixedcase"))
        assert found is not None
        assert found.username.value == "MixedCase"  # original casing preserved


class TestGetByEmail:
    async def test_returns_none_when_absent(self, repository: UserRepository) -> None:
        assert await repository.get_by_email(Email("nobody@example.com")) is None

    async def test_finds_regardless_of_case(self, repository: UserRepository) -> None:
        await repository.create(make_user(email="Person@Example.com"))

        found = await repository.get_by_email(Email("person@example.com"))
        assert found is not None


class TestExists:
    async def test_username_false_then_true(self, repository: UserRepository) -> None:
        assert await repository.exists_by_username(Username("ghost")) is False
        await repository.create(make_user(username="ghost"))
        assert await repository.exists_by_username(Username("ghost")) is True

    async def test_username_is_case_insensitive(self, repository: UserRepository) -> None:
        await repository.create(make_user(username="CaseCheck"))
        assert await repository.exists_by_username(Username("casecheck")) is True

    async def test_email_false_then_true(self, repository: UserRepository) -> None:
        assert await repository.exists_by_email(Email("ghost@example.com")) is False
        await repository.create(make_user(email="ghost@example.com"))
        assert await repository.exists_by_email(Email("ghost@example.com")) is True


# --- update -----------------------------------------------------------------


class TestUpdate:
    async def test_persists_changed_fields(self, repository: UserRepository) -> None:
        user = await repository.create(make_user())

        user.display_name = "New Name"
        user.preferred_language = Locale.UZ
        user.updated_at = _BASE_TIME + timedelta(hours=1)
        await repository.update(user)

        found = await repository.get_by_id(user.id)
        assert found is not None
        assert found.display_name == "New Name"
        assert found.preferred_language is Locale.UZ
        assert found.updated_at == _BASE_TIME + timedelta(hours=1)

    async def test_can_clear_a_nullable_field(self, repository: UserRepository) -> None:
        user = make_user()
        user.display_name = "Something"
        await repository.create(user)

        user.display_name = None
        await repository.update(user)

        found = await repository.get_by_id(user.id)
        assert found is not None
        assert found.display_name is None

    async def test_deactivation_persists(self, repository: UserRepository) -> None:
        user = await repository.create(make_user())
        user.deactivate()
        await repository.update(user)

        found = await repository.get_by_id(user.id)
        assert found is not None
        assert found.is_active is False

    async def test_renaming_onto_another_users_username_raises(
        self, repository: UserRepository
    ) -> None:
        await repository.create(make_user(username="first", email="first@example.com"))
        second = await repository.create(make_user(username="second", email="second@example.com"))

        second.username = Username("FIRST")
        with pytest.raises(UsernameAlreadyExists):
            await repository.update(second)


# --- delete -----------------------------------------------------------------


class TestDelete:
    async def test_returns_false_when_absent(self, repository: UserRepository) -> None:
        assert await repository.delete(uuid4()) is False

    async def test_removes_the_user(self, repository: UserRepository) -> None:
        user = await repository.create(make_user())

        assert await repository.delete(user.id) is True
        assert await repository.get_by_id(user.id) is None


# --- list / keyset pagination ------------------------------------------------


class TestList:
    async def _seed(self, repository: UserRepository, count: int) -> list[User]:
        created = []
        for index in range(count):
            created.append(
                await repository.create(
                    make_user(
                        username=f"player_{index}",
                        email=f"player{index}@example.com",
                        # Distinct timestamps so ordering is unambiguous.
                        created_at=_BASE_TIME + timedelta(minutes=index),
                    )
                )
            )
        return created

    async def test_empty(self, repository: UserRepository) -> None:
        users, page = await repository.list(CursorPageParams(limit=10))
        assert users == []
        assert page.has_more is False
        assert page.next_cursor is None

    async def test_first_page_is_ordered_by_creation(self, repository: UserRepository) -> None:
        await self._seed(repository, 5)

        users, page = await repository.list(CursorPageParams(limit=2))
        assert [user.username.value for user in users] == ["player_0", "player_1"]
        assert page.has_more is True
        assert page.next_cursor is not None

    async def test_walking_every_page_visits_each_user_once(
        self, repository: UserRepository
    ) -> None:
        await self._seed(repository, 7)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):  # bounded: a paging bug must not hang the suite
            users, page = await repository.list(CursorPageParams(limit=3, cursor=cursor))
            seen.extend(user.username.value for user in users)
            if not page.has_more:
                break
            cursor = page.next_cursor

        assert seen == [f"player_{index}" for index in range(7)]

    async def test_final_page_reports_no_more(self, repository: UserRepository) -> None:
        await self._seed(repository, 2)

        users, page = await repository.list(CursorPageParams(limit=10))
        assert len(users) == 2
        assert page.has_more is False
        assert page.next_cursor is None

    async def test_filters_by_is_active(self, repository: UserRepository) -> None:
        await repository.create(make_user(username="active_one", email="a@example.com"))
        inactive = make_user(username="inactive_one", email="b@example.com")
        inactive.deactivate()
        await repository.create(inactive)

        active_users, _ = await repository.list(CursorPageParams(limit=10), is_active=True)
        inactive_users, _ = await repository.list(CursorPageParams(limit=10), is_active=False)

        assert [user.username.value for user in active_users] == ["active_one"]
        assert [user.username.value for user in inactive_users] == ["inactive_one"]


# --- properties only the real database can prove -----------------------------


class TestPostgresSpecificGuarantees:
    """Assertions that are meaningless against the fake, so they take the
    real session directly rather than the parametrised `repository`."""

    async def test_generated_column_matches_python_folding(
        self, contract_session: AsyncSession
    ) -> None:
        """The one that would otherwise rot silently.

        `username_folded` is computed by PostgreSQL as
        `lower(normalize(username, NFKC))`; `domain.validators.fold_username`
        computes the same thing in Python. They are two implementations of
        one rule in two languages — exactly the drift AD-14 describes — and
        if they diverge, the service's uniqueness pre-check and the unique
        constraint reach different verdicts. This asserts they agree on
        inputs chosen to stress the difference.
        """
        from app.modules.users.domain.validators import fold_username

        # Inserted as raw SQL, deliberately bypassing `validate_username`.
        # The property under test is "PostgreSQL's expression and Python's
        # function fold identically", which is about the *folding rule*,
        # not about which handles the application currently admits. Going
        # through the validator would restrict the samples to ASCII and
        # leave the NFKC half of the expression — the half most likely to
        # diverge — completely unexercised.
        samples = [
            "Simple",
            "MiXeD",
            "UPPERCASE",
            "with-hyphen",
            "under_score",
            "ＦｕｌｌＷｉｄｔｈ",  # NFKC compatibility forms
            "Ünïcödé",  # non-ASCII with case
            "Кириллица",  # Cyrillic, per domain-model.md §14.6's locales
        ]

        for index, sample in enumerate(samples):
            await contract_session.execute(
                text(
                    'INSERT INTO users."user" '
                    "(id, username, email, password_hash, preferred_language, timezone,"
                    " is_active, is_verified, created_at) "
                    "VALUES (gen_random_uuid(), :username, :email, 'h', 'en', 'UTC',"
                    " true, false, now())"
                ),
                {"username": sample, "email": f"fold{index}@example.com"},
            )

        rows = (
            await contract_session.execute(
                text('SELECT username, username_folded FROM users."user"')
            )
        ).all()

        assert len(rows) == len(samples)
        for username, folded in rows:
            assert folded == fold_username(username), (
                f"PostgreSQL folded {username!r} to {folded!r}, "
                f"Python folds it to {fold_username(username)!r}"
            )

    async def test_username_policy_rejects_non_ascii_for_now(self) -> None:
        """Pins the interim ASCII-only restriction so that relaxing it is
        a deliberate change with a failing test to update, not something
        that slips in — see `validators._USERNAME_PATTERN` for why it is
        gated on the confusable-skeleton work."""
        from app.modules.users.domain.exceptions import InvalidUsername

        for rejected in ("Кириллица", "Ünïcödé", "ＦｕｌｌＷｉｄｔｈ"):
            with pytest.raises(InvalidUsername):
                Username(rejected)

    async def test_check_constraint_rejects_a_short_username(
        self, contract_session: AsyncSession
    ) -> None:
        """The domain refuses a 2-character username, but the database is
        the authoritative guard (BE-06) — this proves the constraint is
        really there rather than trusting the model declaration."""
        from sqlalchemy.exc import IntegrityError

        # Raw SQL executes immediately, so the violation surfaces on
        # `execute` — not on a later `flush`, which is where an ORM insert
        # would raise it.
        with pytest.raises(IntegrityError, match="ck_user__username_length"):
            await contract_session.execute(
                text(
                    'INSERT INTO users."user" '
                    "(id, username, email, password_hash, preferred_language, timezone, "
                    " is_active, is_verified, created_at) "
                    "VALUES (gen_random_uuid(), 'ab', 'short@example.com', 'h', 'en', 'UTC', "
                    " true, false, now())"
                )
            )
