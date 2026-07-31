"""The `VerificationTokenRepository` contract — repositories.md RP-05.

One suite, two implementations: `FakeVerificationTokenRepository` and
`SqlAlchemyVerificationTokenRepository`. That is the point of RP-05 — "a
fake that has quietly diverged from its real adapter produces the worst
possible test outcome: a green suite over broken behaviour."

It matters here because the two express the same rule in two languages:
the real adapter gets "at most one live token per user" from a partial
unique index and "first consumption wins" from a `WHERE` clause, while
the fake reproduces both in Python. This file is what keeps them
agreeing.

The real half is skipped, not failed, when PostgreSQL is unreachable.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Locale
from app.core.exceptions import ConflictError
from app.modules.auth.application.ports import VerificationTokenRepository
from app.modules.auth.domain.verification import EmailVerificationToken
from app.modules.auth.infrastructure import SqlAlchemyVerificationTokenRepository
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import Email, Timezone, Username
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from tests.fakes.verification_token_repository import FakeVerificationTokenRepository

_BASE_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
ONE_DAY = timedelta(hours=24)


@pytest_asyncio.fixture
async def fake_repository() -> AsyncIterator[VerificationTokenRepository]:
    yield FakeVerificationTokenRepository()


@pytest_asyncio.fixture
async def sqlalchemy_repository(
    contract_session: AsyncSession,
) -> AsyncIterator[VerificationTokenRepository]:
    yield SqlAlchemyVerificationTokenRepository(contract_session)


@pytest.fixture(params=["fake", "sqlalchemy"])
def repository(request: pytest.FixtureRequest) -> VerificationTokenRepository:
    return request.getfixturevalue(f"{request.param}_repository")  # type: ignore[no-any-return]


@pytest_asyncio.fixture
async def user_id(request: pytest.FixtureRequest, repository: VerificationTokenRepository) -> UUID:
    """A real user row against PostgreSQL, a bare UUID against the fake —
    the foreign key means the real adapter cannot store a token for a user
    that does not exist."""
    if isinstance(repository, FakeVerificationTokenRepository):
        return uuid4()

    session: AsyncSession = request.getfixturevalue("contract_session")
    created = await SqlAlchemyUserRepository(session).create(
        User.create(
            username=Username(f"player{uuid4().hex[:8]}"),
            email=Email(f"{uuid4().hex[:12]}@example.com"),
            password_hash="argon2id$fake$notarealhash",
            preferred_language=Locale.EN,
            timezone=Timezone("UTC"),
            created_at=_BASE_TIME,
        )
    )
    return created.id


def make_token(
    *,
    user_id: UUID,
    seed: str = "token",
    issued_at: datetime | None = None,
    lifetime: timedelta = ONE_DAY,
) -> EmailVerificationToken:
    """The "hash" is the seed padded to 32 bytes — the column has a
    `CHECK octet_length(...) = 32`, so a shorter value would be rejected
    by PostgreSQL and accepted by the fake, which is exactly the
    divergence this suite exists to prevent."""
    return EmailVerificationToken.issue(
        user_id=user_id,
        token_hash=seed.encode().ljust(32, b"\x00")[:32],
        issued_at=issued_at or _BASE_TIME,
        lifetime=lifetime,
    )


class TestCreate:
    async def test_persists_and_round_trips(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        created = await repository.create(make_token(user_id=user_id))

        found = await repository.get_by_hash(created.token_hash)
        assert found is not None
        assert found.id == created.id
        assert found.user_id == user_id
        assert found.expires_at == created.expires_at
        assert found.used_at is None

    async def test_timestamps_come_back_timezone_aware(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        """A naive value would raise `TypeError` the moment the service
        compared it against an aware `now()`."""
        created = await repository.create(make_token(user_id=user_id))

        found = await repository.get_by_hash(created.token_hash)
        assert found is not None
        assert found.created_at.tzinfo is not None
        assert found.expires_at.tzinfo is not None

    async def test_two_tokens_cannot_share_a_digest(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        await repository.create(make_token(user_id=user_id, seed="same"))
        await repository.invalidate_active_for_user(user_id, at=_BASE_TIME)

        with pytest.raises(ConflictError):
            await repository.create(make_token(user_id=user_id, seed="same"))

    async def test_a_second_live_token_for_one_user_is_refused(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        """§4.5's "at most one live token per account", enforced by a
        partial unique index in the real adapter. Without it, "invalidate
        the previous token" would be an intention rather than a
        guarantee."""
        await repository.create(make_token(user_id=user_id, seed="first"))

        with pytest.raises(ConflictError):
            await repository.create(make_token(user_id=user_id, seed="second"))

    async def test_a_new_token_is_allowed_once_the_old_one_is_used(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        """The resend path. The partial index only covers unused rows, so
        invalidating first is what makes reissue possible."""
        await repository.create(make_token(user_id=user_id, seed="first"))
        await repository.invalidate_active_for_user(user_id, at=_BASE_TIME)

        created = await repository.create(make_token(user_id=user_id, seed="second"))

        assert created.used_at is None


class TestGetByHash:
    async def test_returns_none_for_an_unknown_digest(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        await repository.create(make_token(user_id=user_id))

        assert await repository.get_by_hash(b"\xff" * 32) is None

    async def test_returns_used_tokens(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        """The contract's least obvious clause: replay detection needs to
        *find* the used token the caller presented. A repository that
        filtered them would make one-time use unenforceable."""
        created = await repository.create(make_token(user_id=user_id))
        await repository.invalidate_active_for_user(user_id, at=_BASE_TIME)

        found = await repository.get_by_hash(created.token_hash)

        assert found is not None
        assert found.is_used is True

    async def test_returns_expired_tokens(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        """Expiry is the service's judgement, not the port's."""
        created = await repository.create(
            make_token(user_id=user_id, lifetime=timedelta(seconds=1))
        )

        assert await repository.get_by_hash(created.token_hash) is not None


class TestInvalidateActiveForUser:
    async def test_marks_the_live_token_used_and_counts_it(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        created = await repository.create(make_token(user_id=user_id))

        assert await repository.invalidate_active_for_user(user_id, at=_BASE_TIME) == 1

        found = await repository.get_by_hash(created.token_hash)
        assert found is not None
        assert found.used_at == _BASE_TIME

    async def test_is_idempotent(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        """Counts only what it actually invalidated — the same
        `WHERE used_at IS NULL` that makes "first consumption wins" hold."""
        await repository.create(make_token(user_id=user_id))
        await repository.invalidate_active_for_user(user_id, at=_BASE_TIME)

        assert await repository.invalidate_active_for_user(user_id, at=_BASE_TIME) == 0

    async def test_the_first_consumption_instant_is_kept(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        """A replay overwriting `used_at` would erase the only record of
        when the real redemption happened."""
        created = await repository.create(make_token(user_id=user_id))
        await repository.invalidate_active_for_user(user_id, at=_BASE_TIME)

        await repository.invalidate_active_for_user(user_id, at=_BASE_TIME + timedelta(hours=1))

        found = await repository.get_by_hash(created.token_hash)
        assert found is not None
        assert found.used_at == _BASE_TIME

    async def test_returns_zero_when_there_is_nothing_to_invalidate(
        self, repository: VerificationTokenRepository
    ) -> None:
        assert await repository.invalidate_active_for_user(uuid4(), at=_BASE_TIME) == 0

    async def test_does_not_touch_another_user(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        created = await repository.create(make_token(user_id=user_id))

        await repository.invalidate_active_for_user(uuid4(), at=_BASE_TIME)

        found = await repository.get_by_hash(created.token_hash)
        assert found is not None
        assert found.is_used is False


class TestCountActiveForUser:
    async def test_counts_the_live_token(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        await repository.create(make_token(user_id=user_id))

        assert await repository.count_active_for_user(user_id, at=_BASE_TIME) == 1

    async def test_excludes_used_tokens(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        await repository.create(make_token(user_id=user_id))
        await repository.invalidate_active_for_user(user_id, at=_BASE_TIME)

        assert await repository.count_active_for_user(user_id, at=_BASE_TIME) == 0

    async def test_excludes_expired_tokens(
        self, repository: VerificationTokenRepository, user_id: UUID
    ) -> None:
        await repository.create(make_token(user_id=user_id))

        assert (
            await repository.count_active_for_user(user_id, at=_BASE_TIME + timedelta(days=2)) == 0
        )

    async def test_is_zero_for_a_user_with_no_tokens(
        self, repository: VerificationTokenRepository
    ) -> None:
        assert await repository.count_active_for_user(uuid4(), at=_BASE_TIME) == 0
