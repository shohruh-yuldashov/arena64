"""The `SessionRepository` contract — repositories.md RP-05.

**One suite, two implementations.** Every test below runs twice: once
against `FakeSessionRepository` (in-memory) and once against
`SqlAlchemySessionRepository` (real PostgreSQL 17). That is the entire
point of RP-05 — "a fake that has quietly diverged from its real adapter
produces the worst possible test outcome: a green suite over broken
behaviour."

It matters more here than for `users`. The real adapter's revocation
methods are Core `UPDATE`s with the condition in a `WHERE` clause, while
the fake reproduces that condition in Python. Those are two different
expressions of "first revocation wins", written in two languages, and
this file is the only thing that keeps them agreeing.

The real-adapter half is skipped, not failed, when PostgreSQL is
unreachable (see `conftest.py`).

Every test that needs a `user_id` creates a real `users.user` row first,
because `auth.user_sessions.user_id` carries a foreign key — a deliberate,
documented deviation from DB-03 (see the model). The fake does not
enforce it, which is fine: the FK is a property of the real adapter that
the contract does not claim the fake reproduces.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Locale
from app.modules.auth.application.ports import SessionRepository
from app.modules.auth.domain.sessions import RevocationReason, SessionDevice, UserSession
from app.modules.auth.infrastructure import SqlAlchemySessionRepository
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import Email, Timezone, Username
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from tests.fakes.session_repository import FakeSessionRepository

_BASE_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
THIRTY_DAYS = timedelta(days=30)


# --- the two implementations under test -------------------------------------


@pytest_asyncio.fixture
async def fake_repository() -> AsyncIterator[SessionRepository]:
    yield FakeSessionRepository()


@pytest_asyncio.fixture
async def sqlalchemy_repository(contract_session: AsyncSession) -> AsyncIterator[SessionRepository]:
    yield SqlAlchemySessionRepository(contract_session)


@pytest.fixture(params=["fake", "sqlalchemy"])
def repository(request: pytest.FixtureRequest) -> SessionRepository:
    """Parametrised over both adapters — this is what makes every test in
    this file run twice. `getfixturevalue` rather than two copies of each
    test, so there is exactly one place the contract is stated."""
    fixture_name = f"{request.param}_repository"
    return request.getfixturevalue(fixture_name)  # type: ignore[no-any-return]


@pytest_asyncio.fixture
async def user_id(request: pytest.FixtureRequest, repository: SessionRepository) -> UUID:
    """A real user row when running against PostgreSQL, a bare UUID when
    running against the fake.

    The foreign key means the real adapter cannot store a session for a
    user that does not exist — which is the point of having it, and which
    a test would otherwise trip over on its first `INSERT`.
    """
    if isinstance(repository, FakeSessionRepository):
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


def make_session(
    *,
    user_id: UUID,
    token: str = "token",
    created_at: datetime | None = None,
    lifetime: timedelta = THIRTY_DAYS,
    token_family: UUID | None = None,
    device: SessionDevice | None = None,
) -> UserSession:
    """A valid session with everything but the field under test defaulted.

    The "hash" is the token padded to 32 bytes — the column has a
    `CHECK octet_length(...) = 32`, so a shorter value would be rejected
    by PostgreSQL and silently accepted by the fake, which is exactly the
    divergence this suite exists to prevent.
    """
    return UserSession.start(
        user_id=user_id,
        refresh_token_hash=token.encode().ljust(32, b"\x00")[:32],
        issued_at=created_at or _BASE_TIME,
        lifetime=lifetime,
        device=device,
        token_family=token_family,
    )


class TestCreateSession:
    async def test_persists_and_returns_the_session(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        created = await repository.create_session(make_session(user_id=user_id))

        found = await repository.get_by_id(created.id)
        assert found is not None
        assert found.id == created.id
        assert found.user_id == user_id

    async def test_round_trips_every_field(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        device = SessionDevice(
            device_name="Chrome on macOS",
            user_agent="Mozilla/5.0 (Macintosh)",
            ip_address="203.0.113.7",
        )
        created = await repository.create_session(make_session(user_id=user_id, device=device))

        found = await repository.get_by_id(created.id)
        assert found is not None
        assert found.device == device
        assert found.token_family == created.token_family
        assert found.expires_at == created.expires_at
        assert found.last_used_at == created.last_used_at
        assert found.revoked_at is None
        assert found.revoked_reason is None

    async def test_timestamps_come_back_timezone_aware(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """A naive value would raise `TypeError` the moment the service
        compared it against an aware `now()` — failing every refresh
        rather than only the expired ones."""
        created = await repository.create_session(make_session(user_id=user_id))

        found = await repository.get_by_id(created.id)
        assert found is not None
        assert found.created_at.tzinfo is not None
        assert found.expires_at.tzinfo is not None
        assert found.last_used_at.tzinfo is not None

    async def test_a_session_with_no_device_is_valid(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """A client that sends no `User-Agent` still gets a session."""
        created = await repository.create_session(make_session(user_id=user_id, device=None))

        found = await repository.get_by_id(created.id)
        assert found is not None
        assert found.device.device_name is None
        assert found.device.ip_address is None


class TestGetSession:
    async def test_finds_a_session_by_its_token_hash(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        created = await repository.create_session(make_session(user_id=user_id, token="alpha"))

        found = await repository.get_session(created.refresh_token_hash)

        assert found is not None
        assert found.id == created.id

    async def test_returns_none_for_an_unknown_hash(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        await repository.create_session(make_session(user_id=user_id))

        assert await repository.get_session(b"\xff" * 32) is None

    async def test_returns_revoked_sessions(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """The contract's least obvious clause, and the one reuse
        detection depends on: detecting reuse *requires* finding the
        revoked session the attacker presented. A repository that filtered
        revoked rows would make it impossible."""
        created = await repository.create_session(make_session(user_id=user_id))
        await repository.revoke_session(created.id, at=_BASE_TIME, reason=RevocationReason.PLAYER)

        found = await repository.get_session(created.refresh_token_hash)

        assert found is not None
        assert found.is_revoked is True

    async def test_returns_expired_sessions(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """Expiry is the service's judgement, not the port's."""
        created = await repository.create_session(
            make_session(user_id=user_id, lifetime=timedelta(seconds=1))
        )

        assert await repository.get_session(created.refresh_token_hash) is not None

    async def test_get_by_id_returns_none_when_absent(self, repository: SessionRepository) -> None:
        assert await repository.get_by_id(uuid4()) is None


class TestUpdateLastUsed:
    async def test_slides_the_window(self, repository: SessionRepository, user_id: UUID) -> None:
        created = await repository.create_session(make_session(user_id=user_id))
        later = _BASE_TIME + timedelta(days=3)

        assert await repository.update_last_used(created.id, later) is True

        found = await repository.get_by_id(created.id)
        assert found is not None
        assert found.last_used_at == later

    async def test_does_not_extend_the_absolute_expiry(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """What makes the 30-day bound absolute."""
        created = await repository.create_session(make_session(user_id=user_id))

        await repository.update_last_used(created.id, _BASE_TIME + timedelta(days=20))

        found = await repository.get_by_id(created.id)
        assert found is not None
        assert found.expires_at == created.expires_at

    async def test_refuses_a_revoked_session(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """Otherwise a revoked session's "last seen" keeps moving, and the
        revocation list lies about when the device was last used."""
        created = await repository.create_session(make_session(user_id=user_id))
        await repository.revoke_session(created.id, at=_BASE_TIME, reason=RevocationReason.PLAYER)

        assert (
            await repository.update_last_used(created.id, _BASE_TIME + timedelta(days=1)) is False
        )

    async def test_returns_false_for_an_unknown_session(
        self, repository: SessionRepository
    ) -> None:
        assert await repository.update_last_used(uuid4(), _BASE_TIME) is False


class TestRevokeSession:
    async def test_records_when_and_why(self, repository: SessionRepository, user_id: UUID) -> None:
        created = await repository.create_session(make_session(user_id=user_id))

        assert (
            await repository.revoke_session(
                created.id, at=_BASE_TIME, reason=RevocationReason.SUSPENSION
            )
            is True
        )

        found = await repository.get_by_id(created.id)
        assert found is not None
        assert found.revoked_at == _BASE_TIME
        assert found.revoked_reason is RevocationReason.SUSPENSION

    async def test_the_first_revocation_wins(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """Expressed twice — as `WHERE revoked_at IS NULL` in SQL and as an
        early return in Python — so this is the assertion that keeps the
        two agreeing. A `reuse_detected` overwritten by a later `player`
        would erase the only record that an attack was found."""
        created = await repository.create_session(make_session(user_id=user_id))
        await repository.revoke_session(
            created.id, at=_BASE_TIME, reason=RevocationReason.REUSE_DETECTED
        )

        applied = await repository.revoke_session(
            created.id, at=_BASE_TIME + timedelta(hours=1), reason=RevocationReason.PLAYER
        )

        assert applied is False
        found = await repository.get_by_id(created.id)
        assert found is not None
        assert found.revoked_reason is RevocationReason.REUSE_DETECTED
        assert found.revoked_at == _BASE_TIME

    async def test_returns_false_for_an_unknown_session(
        self, repository: SessionRepository
    ) -> None:
        assert (
            await repository.revoke_session(uuid4(), at=_BASE_TIME, reason=RevocationReason.PLAYER)
            is False
        )

    async def test_leaves_other_sessions_alone(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        target = await repository.create_session(make_session(user_id=user_id, token="a"))
        survivor = await repository.create_session(make_session(user_id=user_id, token="b"))

        await repository.revoke_session(target.id, at=_BASE_TIME, reason=RevocationReason.PLAYER)

        found = await repository.get_by_id(survivor.id)
        assert found is not None
        assert found.is_revoked is False


class TestRevokeAllSessions:
    async def test_revokes_every_live_session_and_counts_them(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        for token in ("a", "b", "c"):
            await repository.create_session(make_session(user_id=user_id, token=token))

        revoked = await repository.revoke_all_sessions(
            user_id, at=_BASE_TIME, reason=RevocationReason.SUSPENSION
        )

        assert revoked == 3
        assert await repository.list_user_sessions(user_id) == []

    async def test_keeps_the_excepted_session(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """SE-1 — a password change revokes every session except the one
        performing it."""
        keep = await repository.create_session(make_session(user_id=user_id, token="a"))
        await repository.create_session(make_session(user_id=user_id, token="b"))

        revoked = await repository.revoke_all_sessions(
            user_id,
            at=_BASE_TIME,
            reason=RevocationReason.PASSWORD_CHANGE,
            except_session_id=keep.id,
        )

        assert revoked == 1
        remaining = await repository.list_user_sessions(user_id)
        assert [session.id for session in remaining] == [keep.id]

    async def test_counts_only_what_it_actually_revoked(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """Already-revoked rows are excluded by the same
        `WHERE revoked_at IS NULL`, so the count is the honest number of
        sessions this call ended rather than the number it looked at."""
        already = await repository.create_session(make_session(user_id=user_id, token="a"))
        await repository.create_session(make_session(user_id=user_id, token="b"))
        await repository.revoke_session(already.id, at=_BASE_TIME, reason=RevocationReason.PLAYER)

        revoked = await repository.revoke_all_sessions(
            user_id, at=_BASE_TIME, reason=RevocationReason.SUSPENSION
        )

        assert revoked == 1

    async def test_is_idempotent(self, repository: SessionRepository, user_id: UUID) -> None:
        await repository.create_session(make_session(user_id=user_id))
        await repository.revoke_all_sessions(user_id, at=_BASE_TIME, reason=RevocationReason.PLAYER)

        assert (
            await repository.revoke_all_sessions(
                user_id, at=_BASE_TIME, reason=RevocationReason.PLAYER
            )
            == 0
        )

    async def test_returns_zero_when_there_is_nothing_to_revoke(
        self, repository: SessionRepository
    ) -> None:
        """SE-3 must work for a suspended account with no live session."""
        assert (
            await repository.revoke_all_sessions(
                uuid4(), at=_BASE_TIME, reason=RevocationReason.SUSPENSION
            )
            == 0
        )


class TestRevokeFamily:
    async def test_revokes_the_whole_chain(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """database.md §14.3's reuse response."""
        root = await repository.create_session(make_session(user_id=user_id, token="a"))
        await repository.create_session(
            make_session(user_id=user_id, token="b", token_family=root.token_family)
        )

        revoked = await repository.revoke_family(
            root.token_family, at=_BASE_TIME, reason=RevocationReason.REUSE_DETECTED
        )

        assert revoked == 2
        assert await repository.list_user_sessions(user_id) == []

    async def test_leaves_other_families_alone(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        """Different blast radius from `revoke_all_sessions`: signing
        someone out of their phone because their laptop's token was
        replayed is worse than the attack in most cases."""
        compromised = await repository.create_session(make_session(user_id=user_id, token="a"))
        other = await repository.create_session(make_session(user_id=user_id, token="b"))

        await repository.revoke_family(
            compromised.token_family, at=_BASE_TIME, reason=RevocationReason.REUSE_DETECTED
        )

        found = await repository.get_by_id(other.id)
        assert found is not None
        assert found.is_revoked is False

    async def test_returns_zero_for_an_unknown_family(self, repository: SessionRepository) -> None:
        assert (
            await repository.revoke_family(
                uuid4(), at=_BASE_TIME, reason=RevocationReason.REUSE_DETECTED
            )
            == 0
        )


class TestListUserSessions:
    async def test_excludes_revoked_by_default(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        live = await repository.create_session(make_session(user_id=user_id, token="a"))
        revoked = await repository.create_session(make_session(user_id=user_id, token="b"))
        await repository.revoke_session(revoked.id, at=_BASE_TIME, reason=RevocationReason.PLAYER)

        listed = await repository.list_user_sessions(user_id)

        assert [session.id for session in listed] == [live.id]

    async def test_includes_revoked_on_request(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        await repository.create_session(make_session(user_id=user_id, token="a"))
        revoked = await repository.create_session(make_session(user_id=user_id, token="b"))
        await repository.revoke_session(revoked.id, at=_BASE_TIME, reason=RevocationReason.PLAYER)

        assert len(await repository.list_user_sessions(user_id, include_revoked=True)) == 2

    async def test_is_newest_first(self, repository: SessionRepository, user_id: UUID) -> None:
        older = await repository.create_session(
            make_session(user_id=user_id, token="a", created_at=_BASE_TIME)
        )
        newer = await repository.create_session(
            make_session(user_id=user_id, token="b", created_at=_BASE_TIME + timedelta(minutes=5))
        )

        listed = await repository.list_user_sessions(user_id)

        assert [session.id for session in listed] == [newer.id, older.id]

    async def test_is_scoped_to_one_user(
        self, repository: SessionRepository, user_id: UUID
    ) -> None:
        await repository.create_session(make_session(user_id=user_id))

        assert await repository.list_user_sessions(uuid4()) == []

    async def test_is_empty_for_a_user_with_no_sessions(
        self, repository: SessionRepository
    ) -> None:
        assert await repository.list_user_sessions(uuid4()) == []


class TestPostgresSpecificGuarantees:
    """Properties only the real adapter can demonstrate — the database
    constraints that BE-06 makes authoritative. The fake does not claim
    to reproduce these, so they are not part of the shared contract."""

    async def test_two_sessions_cannot_share_a_token_hash(
        self, sqlalchemy_repository: SessionRepository, contract_session: AsyncSession
    ) -> None:
        """`uq_user_sessions__refresh_token_hash`. Two rows sharing a hash
        would mean one presented token matching two sessions, and no
        correct behaviour exists for that."""
        from app.core.exceptions import ConflictError

        user = await _persist_user(contract_session)
        await sqlalchemy_repository.create_session(make_session(user_id=user, token="same"))

        with pytest.raises(ConflictError):
            await sqlalchemy_repository.create_session(make_session(user_id=user, token="same"))

    async def test_the_foreign_key_rejects_an_unknown_user(
        self, sqlalchemy_repository: SessionRepository
    ) -> None:
        """The deliberate DB-03 deviation, doing its job: a session for a
        user that does not exist is a credential with no owner."""
        from sqlalchemy.exc import IntegrityError

        with pytest.raises((IntegrityError, Exception)):
            await sqlalchemy_repository.create_session(make_session(user_id=uuid4()))

    async def test_deleting_the_user_cascades_to_their_sessions(
        self, sqlalchemy_repository: SessionRepository, contract_session: AsyncSession
    ) -> None:
        """`ON DELETE CASCADE` — the reason the FK is worth the deviation.
        Without it, erasing an account would leave live sessions behind
        that could still be refreshed."""
        from sqlalchemy import text

        user = await _persist_user(contract_session)
        created = await sqlalchemy_repository.create_session(make_session(user_id=user))

        await contract_session.execute(
            text('DELETE FROM users."user" WHERE id = :id'), {"id": user}
        )

        assert await sqlalchemy_repository.get_by_id(created.id) is None

    async def test_a_half_revoked_row_is_impossible(self, contract_session: AsyncSession) -> None:
        """§4.4's constraint: `revoked_at` is set if and only if
        `revoked_reason` is. A row revoked with no reason is one no
        application logic knows how to read."""
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        user = await _persist_user(contract_session)
        created = await SqlAlchemySessionRepository(contract_session).create_session(
            make_session(user_id=user)
        )

        with pytest.raises(IntegrityError):
            await contract_session.execute(
                text("UPDATE auth.user_sessions SET revoked_at = now() WHERE id = :id"),
                {"id": created.id},
            )


async def _persist_user(session: AsyncSession) -> UUID:
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
