"""`UserService` — application-layer tests, no database.

Runs against `FakeUserRepository` and a fixed clock, which is the whole
point of the port/adapter split (repositories.md RP-05, AD-07): these
assert *orchestration* — which rules fire, in what order, what is raised —
and they do it in milliseconds without Docker. The fake is held to the
same contract as the real adapter in `tests/contract/test_user_repository.py`,
so "passes here" means something.
"""

from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import uuid4

import pytest

from app.core.pagination import CursorPageParams
from app.modules.users.application.commands import CreateUser, UpdateUserProfile
from app.modules.users.application.services import UserService
from app.modules.users.domain.exceptions import (
    EmailAlreadyExists,
    InvalidEmail,
    InvalidLanguage,
    InvalidTimezone,
    InvalidUsername,
    UsernameAlreadyExists,
    UserNotFound,
)
from tests.fakes.user_repository import FakeUserRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FixedClock:
    """A clock that does not move unless a test moves it — AD-07's payoff.
    Asserting `updated_at` against a real `datetime.now()` would either be
    flaky or require sleeping."""

    def __init__(self, now: datetime = _NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class RecordingUnitOfWork:
    """Counts commits so a test can assert a write path actually committed
    — a service that mutates an entity but never commits passes every
    assertion about the returned object and silently loses the write."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rollbacks += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def repository() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def unit_of_work() -> RecordingUnitOfWork:
    return RecordingUnitOfWork()


@pytest.fixture
def service(
    repository: FakeUserRepository,
    unit_of_work: RecordingUnitOfWork,
    clock: FixedClock,
) -> UserService:
    return UserService(users=repository, unit_of_work=unit_of_work, clock=clock)


def create_command(
    *,
    username: str = "player_one",
    email: str = "player.one@example.com",
    language: str = "en",
    timezone: str = "UTC",
) -> CreateUser:
    return CreateUser(
        username=username,
        email=email,
        password_hash="argon2id$fake$notarealhash",
        preferred_language=language,
        timezone=timezone,
    )


class TestCreateUser:
    async def test_creates_an_active_unverified_user(self, service: UserService) -> None:
        user = await service.create_user(create_command())

        assert user.is_active is True
        # Nobody has proven they own the address — proving it is `auth`'s
        # job, and a user that arrived verified would skip it entirely.
        assert user.is_verified is False

    async def test_stamps_created_at_from_the_injected_clock(self, service: UserService) -> None:
        user = await service.create_user(create_command())
        assert user.created_at == _NOW
        assert user.updated_at is None

    async def test_commits(self, service: UserService, unit_of_work: RecordingUnitOfWork) -> None:
        await service.create_user(create_command())
        assert unit_of_work.commits == 1

    async def test_does_not_hash_the_password(self, service: UserService) -> None:
        # The task's constraint, asserted rather than assumed: whatever is
        # handed in is stored verbatim. A service that quietly hashed here
        # would double-hash whatever `auth` sends it.
        command = create_command()
        user = await service.create_user(command)
        assert user.password_hash == command.password_hash

    async def test_normalises_the_email(self, service: UserService) -> None:
        user = await service.create_user(create_command(email="  Player@Example.COM "))
        assert user.email.value == "player@example.com"

    async def test_preserves_username_casing(self, service: UserService) -> None:
        user = await service.create_user(create_command(username="PlayerOne"))
        assert user.username.value == "PlayerOne"

    async def test_rejects_a_duplicate_username(self, service: UserService) -> None:
        await service.create_user(create_command(username="taken", email="a@example.com"))

        with pytest.raises(UsernameAlreadyExists):
            await service.create_user(create_command(username="taken", email="b@example.com"))

    async def test_rejects_a_duplicate_username_case_insensitively(
        self, service: UserService
    ) -> None:
        await service.create_user(create_command(username="Taken", email="a@example.com"))

        with pytest.raises(UsernameAlreadyExists):
            await service.create_user(create_command(username="TAKEN", email="b@example.com"))

    async def test_rejects_a_duplicate_email(self, service: UserService) -> None:
        await service.create_user(create_command(username="one", email="dup@example.com"))

        with pytest.raises(EmailAlreadyExists):
            await service.create_user(create_command(username="two", email="dup@example.com"))

    async def test_does_not_commit_when_a_uniqueness_rule_rejects(
        self, service: UserService, unit_of_work: RecordingUnitOfWork
    ) -> None:
        await service.create_user(create_command(username="taken", email="a@example.com"))
        commits_before = unit_of_work.commits

        with pytest.raises(UsernameAlreadyExists):
            await service.create_user(create_command(username="taken", email="b@example.com"))

        assert unit_of_work.commits == commits_before

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (create_command(username="ab"), InvalidUsername),
            (create_command(username="admin"), InvalidUsername),
            (create_command(email="not-an-email"), InvalidEmail),
            (create_command(language="de"), InvalidLanguage),
            (create_command(timezone="Mars/Olympus"), InvalidTimezone),
        ],
    )
    async def test_rejects_invalid_input(
        self, service: UserService, command: CreateUser, expected: type[Exception]
    ) -> None:
        with pytest.raises(expected):
            await service.create_user(command)


class TestGetUser:
    async def test_returns_the_user(self, service: UserService) -> None:
        created = await service.create_user(create_command())
        assert (await service.get_user(created.id)).id == created.id

    async def test_raises_when_absent(self, service: UserService) -> None:
        with pytest.raises(UserNotFound):
            await service.get_user(uuid4())


class TestLookup:
    async def test_find_by_username_is_case_insensitive(self, service: UserService) -> None:
        created = await service.create_user(create_command(username="MixedCase"))
        assert (await service.find_by_username("mixedcase")).id == created.id

    async def test_find_by_username_raises_when_absent(self, service: UserService) -> None:
        with pytest.raises(UserNotFound):
            await service.find_by_username("nobody_here")

    async def test_find_by_email_is_case_insensitive(self, service: UserService) -> None:
        created = await service.create_user(create_command(email="Person@Example.com"))
        assert (await service.find_by_email("person@example.com")).id == created.id

    async def test_find_by_email_does_not_echo_the_address_in_the_error(
        self, service: UserService
    ) -> None:
        # services.md §8.5: an error message is a place personal data leaks
        # into logs and screenshots.
        with pytest.raises(UserNotFound) as exc_info:
            await service.find_by_email("secret.person@example.com")
        assert "secret.person" not in str(exc_info.value)


class TestUpdateProfile:
    async def test_sets_a_field(self, service: UserService) -> None:
        created = await service.create_user(create_command())

        updated = await service.update_profile(
            created.id, UpdateUserProfile(display_name="New Name")
        )
        assert updated.display_name is not None and updated.display_name.value == "New Name"

    async def test_an_absent_field_is_left_alone(self, service: UserService) -> None:
        created = await service.create_user(create_command())
        await service.update_profile(created.id, UpdateUserProfile(display_name="Keep Me"))

        # Update something else entirely; display_name must survive.
        updated = await service.update_profile(
            created.id, UpdateUserProfile(timezone="Europe/London")
        )
        assert updated.display_name is not None and updated.display_name.value == "Keep Me"
        assert updated.timezone.value == "Europe/London"

    async def test_an_explicit_null_clears_the_field(self, service: UserService) -> None:
        # The distinction `UNSET` exists for: absent leaves alone (above),
        # explicit null clears.
        created = await service.create_user(create_command())
        await service.update_profile(created.id, UpdateUserProfile(display_name="Bye"))

        updated = await service.update_profile(created.id, UpdateUserProfile(display_name=None))
        assert updated.display_name is None

    async def test_stamps_updated_at_from_the_clock(
        self, service: UserService, clock: FixedClock
    ) -> None:
        created = await service.create_user(create_command())
        clock.advance(timedelta(hours=3))

        updated = await service.update_profile(created.id, UpdateUserProfile(display_name="Later"))
        assert updated.updated_at == _NOW + timedelta(hours=3)

    async def test_validates_a_timezone(self, service: UserService) -> None:
        created = await service.create_user(create_command())

        with pytest.raises(InvalidTimezone):
            await service.update_profile(created.id, UpdateUserProfile(timezone="Mars/Olympus"))

    async def test_raises_when_the_user_is_absent(self, service: UserService) -> None:
        with pytest.raises(UserNotFound):
            await service.update_profile(uuid4(), UpdateUserProfile(display_name="x"))

    async def test_an_all_unset_command_still_commits_and_stamps(
        self, service: UserService, unit_of_work: RecordingUnitOfWork, clock: FixedClock
    ) -> None:
        created = await service.create_user(create_command())
        clock.advance(timedelta(minutes=5))
        commits_before = unit_of_work.commits

        updated = await service.update_profile(created.id, UpdateUserProfile())

        # An empty PATCH is a no-op in content but still a write: it is
        # simpler and more predictable than a special "nothing changed"
        # path that would have to decide whether to touch `updated_at`.
        assert unit_of_work.commits == commits_before + 1
        assert updated.updated_at == _NOW + timedelta(minutes=5)


class TestActivation:
    async def test_deactivate(self, service: UserService) -> None:
        created = await service.create_user(create_command())
        assert (await service.deactivate(created.id)).is_active is False

    async def test_activate_after_deactivate(self, service: UserService) -> None:
        created = await service.create_user(create_command())
        await service.deactivate(created.id)
        assert (await service.activate(created.id)).is_active is True

    async def test_deactivate_is_idempotent(
        self, service: UserService, unit_of_work: RecordingUnitOfWork
    ) -> None:
        created = await service.create_user(create_command())
        await service.deactivate(created.id)
        commits_after_first = unit_of_work.commits

        result = await service.deactivate(created.id)

        assert result.is_active is False
        # No second write: a retry after a dropped response must succeed
        # without doing the work twice.
        assert unit_of_work.commits == commits_after_first

    async def test_activate_is_idempotent_on_a_new_user(
        self, service: UserService, unit_of_work: RecordingUnitOfWork
    ) -> None:
        created = await service.create_user(create_command())
        commits_before = unit_of_work.commits

        assert (await service.activate(created.id)).is_active is True
        assert unit_of_work.commits == commits_before

    async def test_raises_when_absent(self, service: UserService) -> None:
        with pytest.raises(UserNotFound):
            await service.deactivate(uuid4())


class TestListUsers:
    async def _seed(self, service: UserService, count: int) -> None:
        for index in range(count):
            await service.create_user(
                create_command(username=f"player_{index}", email=f"p{index}@example.com")
            )

    async def test_empty(self, service: UserService) -> None:
        users, page = await service.list_users(CursorPageParams(limit=10))
        assert users == []
        assert page.has_more is False

    async def test_pages_through_every_user(self, service: UserService) -> None:
        await self._seed(service, 5)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            users, page = await service.list_users(CursorPageParams(limit=2, cursor=cursor))
            seen.extend(user.username.value for user in users)
            if not page.has_more:
                break
            cursor = page.next_cursor

        assert sorted(seen) == sorted(f"player_{index}" for index in range(5))

    async def test_filters_by_active(self, service: UserService) -> None:
        await self._seed(service, 3)
        target = await service.find_by_username("player_1")
        await service.deactivate(target.id)

        active, _ = await service.list_users(CursorPageParams(limit=10), is_active=True)
        inactive, _ = await service.list_users(CursorPageParams(limit=10), is_active=False)

        assert len(active) == 2
        assert [user.username.value for user in inactive] == ["player_1"]

    async def test_listing_opens_no_transaction(
        self, service: UserService, unit_of_work: RecordingUnitOfWork
    ) -> None:
        await self._seed(service, 2)
        commits_before = unit_of_work.commits

        await service.list_users(CursorPageParams(limit=10))

        assert unit_of_work.commits == commits_before
