"""`RegistrationService` — orchestration, no database and no real hashing.

Uses `FakeUserRepository` behind the real `UserService`/`UserAccountService`
stack (so uniqueness and validation are the production code paths) and a
stub hasher (so the suite is not spending 20ms per test proving argon2-cffi
works — `test_password_hasher.py` does that).
"""

from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest
from pydantic import SecretStr

from app.modules.auth.application.commands import NewAccount, RegisterUser
from app.modules.auth.application.services import RegistrationService
from app.modules.auth.domain.exceptions import WeakPassword
from app.modules.users.application.services import UserService
from app.modules.users.application.services.user_account_service import UserAccountService
from app.modules.users.domain.exceptions import (
    EmailAlreadyExists,
    InvalidEmail,
    InvalidUsername,
    UsernameAlreadyExists,
)
from app.modules.users.domain.value_objects import Username
from tests.fakes.user_repository import FakeUserRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


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


class _RecordingHasher:
    """Records what it was asked to hash, so a test can assert the service
    passed the real plaintext through — and returns a marker that is
    obviously not a real hash, so anything comparing against Argon2 output
    fails loudly rather than coincidentally passing."""

    def __init__(self) -> None:
        self.hashed: list[str] = []

    async def hash(self, plaintext: str) -> str:
        self.hashed.append(plaintext)
        return f"stub-hash-of-{len(plaintext)}-chars"


@pytest.fixture
def repository() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def unit_of_work() -> _NullUnitOfWork:
    return _NullUnitOfWork()


@pytest.fixture
def hasher() -> _RecordingHasher:
    return _RecordingHasher()


@pytest.fixture
def service(
    repository: FakeUserRepository,
    unit_of_work: _NullUnitOfWork,
    hasher: _RecordingHasher,
) -> RegistrationService:
    users = UserService(users=repository, unit_of_work=unit_of_work, clock=_FixedClock())
    return RegistrationService(accounts=UserAccountService(users), password_hasher=hasher)


def command(
    *,
    username: str = "player_one",
    email: str = "player.one@example.com",
    password: str = "CorrectHorse1!",
    language: str = "en",
    timezone: str = "UTC",
) -> RegisterUser:
    return RegisterUser(
        username=username,
        email=email,
        password=SecretStr(password),
        preferred_language=language,
        timezone=timezone,
    )


class TestSuccessfulRegistration:
    async def test_returns_the_created_user(self, service: RegistrationService) -> None:
        created = await service.register(command())

        assert created.username == "player_one"
        assert created.email == "player.one@example.com"
        assert created.id is not None

    async def test_the_new_account_is_active_but_unverified(
        self, service: RegistrationService
    ) -> None:
        created = await service.register(command())

        assert created.is_active is True
        # Nobody has proven they own the address — that is a later task.
        assert created.is_verified is False

    async def test_the_returned_shape_has_no_password_field_at_all(
        self, service: RegistrationService
    ) -> None:
        created = await service.register(command())
        dumped = created.model_dump()

        assert "password" not in dumped
        assert "password_hash" not in dumped

    async def test_the_password_is_hashed_not_stored_raw(
        self, service: RegistrationService, repository: FakeUserRepository, hasher: _RecordingHasher
    ) -> None:
        await service.register(command(password="CorrectHorse1!"))

        stored = await repository.get_by_username(Username("player_one"))
        assert stored is not None
        assert stored.password_hash == "stub-hash-of-14-chars"
        assert stored.password_hash != "CorrectHorse1!"
        # The service handed the hasher the real plaintext, unmodified.
        assert hasher.hashed == ["CorrectHorse1!"]

    async def test_commits_exactly_once(
        self, service: RegistrationService, unit_of_work: _NullUnitOfWork
    ) -> None:
        await service.register(command())
        assert unit_of_work.commits == 1

    async def test_normalises_the_email(self, service: RegistrationService) -> None:
        created = await service.register(command(email="  Player.One@Example.COM "))
        assert created.email == "player.one@example.com"

    async def test_preserves_username_casing(self, service: RegistrationService) -> None:
        created = await service.register(command(username="PlayerOne"))
        assert created.username == "PlayerOne"

    async def test_honours_language_and_timezone(self, service: RegistrationService) -> None:
        created = await service.register(command(language="uz", timezone="Asia/Samarkand"))

        assert created.preferred_language.value == "uz"
        assert created.timezone == "Asia/Samarkand"


class TestDuplicateEmail:
    async def test_raises(self, service: RegistrationService) -> None:
        await service.register(command(username="first", email="dup@example.com"))

        with pytest.raises(EmailAlreadyExists) as exc_info:
            await service.register(command(username="second", email="dup@example.com"))
        assert exc_info.value.code == "email_already_exists"

    async def test_is_case_insensitive(self, service: RegistrationService) -> None:
        await service.register(command(username="first", email="Dup@Example.com"))

        with pytest.raises(EmailAlreadyExists):
            await service.register(command(username="second", email="dup@example.com"))

    async def test_does_not_commit(
        self, service: RegistrationService, unit_of_work: _NullUnitOfWork
    ) -> None:
        await service.register(command(username="first", email="dup@example.com"))
        commits_before = unit_of_work.commits

        with pytest.raises(EmailAlreadyExists):
            await service.register(command(username="second", email="dup@example.com"))

        assert unit_of_work.commits == commits_before


class TestDuplicateUsername:
    async def test_raises(self, service: RegistrationService) -> None:
        await service.register(command(username="taken", email="a@example.com"))

        with pytest.raises(UsernameAlreadyExists) as exc_info:
            await service.register(command(username="taken", email="b@example.com"))
        assert exc_info.value.code == "username_already_exists"

    async def test_is_case_insensitive(self, service: RegistrationService) -> None:
        await service.register(command(username="Taken", email="a@example.com"))

        with pytest.raises(UsernameAlreadyExists):
            await service.register(command(username="TAKEN", email="b@example.com"))


class TestInvalidPassword:
    @pytest.mark.parametrize(
        "password",
        [
            "Aa1!",  # too short
            "nouppercase1!",
            "NOLOWERCASE1!",
            "NoDigitsHere!",
            "NoSpecial123x",
        ],
    )
    async def test_raises_weak_password(self, service: RegistrationService, password: str) -> None:
        with pytest.raises(WeakPassword):
            await service.register(command(password=password))

    async def test_nothing_is_created(
        self, service: RegistrationService, repository: FakeUserRepository
    ) -> None:
        with pytest.raises(WeakPassword):
            await service.register(command(password="weak"))

        assert await repository.get_by_username(Username("player_one")) is None

    async def test_a_weak_password_is_never_hashed(
        self, service: RegistrationService, hasher: _RecordingHasher
    ) -> None:
        """Validation runs *before* hashing. Argon2 costs ~19 MiB and ~20ms
        per call, so hashing input that is going to be rejected anyway is
        an amplification primitive on a public endpoint."""
        with pytest.raises(WeakPassword):
            await service.register(command(password="weak"))

        assert hasher.hashed == []


class TestInvalidUsername:
    @pytest.mark.parametrize(
        "username",
        [
            "ab",  # too short
            "a" * 21,  # too long — 20 is the A64-011.1 bound
            "has space",
            "has-hyphen",  # permitted before A64-011.1, not after
            "_leading",
            "admin",  # reserved
            "Кириллица",  # non-ASCII, gated on the confusable work
        ],
    )
    async def test_raises(self, service: RegistrationService, username: str) -> None:
        with pytest.raises(InvalidUsername):
            await service.register(command(username=username))

    async def test_carries_the_invalid_username_code(self, service: RegistrationService) -> None:
        with pytest.raises(InvalidUsername) as exc_info:
            await service.register(command(username="ab"))
        assert exc_info.value.code == "invalid_username"


class TestInvalidEmail:
    @pytest.mark.parametrize("email", ["not-an-email", "no@domain", "@example.com", ""])
    async def test_raises(self, service: RegistrationService, email: str) -> None:
        with pytest.raises(InvalidEmail):
            await service.register(command(email=email))

    async def test_carries_the_invalid_email_code(self, service: RegistrationService) -> None:
        with pytest.raises(InvalidEmail) as exc_info:
            await service.register(command(email="nope"))
        assert exc_info.value.code == "invalid_email"


class TestSecrecy:
    def test_the_command_repr_hides_the_password(self) -> None:
        """`RegisterUser` is exactly the kind of object that lands in a log
        line or an exception repr — `dataclass` prints every field."""
        rendered = repr(command(password="SuperSecret1!"))

        assert "SuperSecret1!" not in rendered
        assert "**********" in rendered

    def test_the_hashed_account_repr_hides_the_hash(self) -> None:
        rendered = repr(
            NewAccount(
                username="u",
                email="e@example.com",
                password_hash="$argon2id$v=19$m=19456,t=2,p=1$abc$def",
                preferred_language="en",
                timezone="UTC",
            )
        )
        assert "argon2id" not in rendered

    async def test_a_failure_message_never_contains_the_password(
        self, service: RegistrationService
    ) -> None:
        with pytest.raises(WeakPassword) as exc_info:
            await service.register(command(password="hunter2hunter2"))

        assert "hunter2" not in str(exc_info.value)
