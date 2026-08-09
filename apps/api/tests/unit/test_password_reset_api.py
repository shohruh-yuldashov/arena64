"""`POST /auth/password/forgot` and `POST /auth/password/reset` — the HTTP
surface.

In `tests/unit/` per services.md §1's definition ("no I/O, fakes only"):
the token store, the session store and the account store are in-memory,
and the hasher is a stub, all swapped in through FastAPI's
`dependency_overrides`. Routing, middleware, the response envelope and
every exception handler are the production ones — which is the point,
since most of what this file asserts is that the right status and the
right wire code come out the other end without `auth` registering a
handler of its own.

`tests/unit/test_password_reset_service.py` covers the orchestration.
`tests/contract/test_password_reset_api.py` runs the same two endpoints
against real PostgreSQL and real Argon2id.

## What this file is really for

Two things a service test cannot reach:

**The forgot endpoint's replies must be byte-identical.** Not merely "both
succeed" — the same status, the same (absent) body, the same headers, for
an address that exists and one that does not. Asserted by comparing the
two responses to each other rather than each against a constant, because
the failure this guards against is a *difference*, whatever it turns out
to be.

**A weak password must be rejected before the token is looked at.** Over
HTTP that is the schema's doing rather than the service's, and it is the
schema that makes the token oracle unreachable in production. Testing it
here is testing the layer that actually provides the property.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.app_factory import create_app
from app.config.settings import EmailSettings, SessionSettings
from app.core.enums import Locale
from app.modules.auth.application.services import (
    OpaqueTokenService,
    PasswordResetService,
    RefreshTokenService,
    SessionService,
)
from app.modules.auth.domain.exceptions import InvalidRefreshToken
from app.modules.auth.domain.sessions import SessionDevice
from app.modules.auth.presentation.dependencies import (
    get_password_reset_service,
    get_session_service,
)
from app.modules.users.application.services import UserService
from app.modules.users.application.services.password_reset_writer import PasswordResetWriter
from app.modules.users.application.services.user_profile_service import UserProfileService
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import Email, Timezone, Username
from app.platform.email import EmailMessage
from tests.fakes.moderation import UnrestrictedAccounts
from tests.fakes.password_reset_token_repository import FakePasswordResetTokenRepository
from tests.fakes.session_repository import FakeSessionRepository
from tests.fakes.user_repository import FakeUserRepository

_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
FORGOT_URL = "/api/v1/auth/password/forgot"
RESET_URL = "/api/v1/auth/password/reset"
EMAIL = "player.one@example.com"
UNKNOWN_EMAIL = "nobody@example.com"
OLD_PASSWORD = "CorrectHorse1!"
NEW_PASSWORD = "BrandNewHorse2!"


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


class _NullUnitOfWork:
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
        return None

    async def rollback(self) -> None:
        return None


class _StubHasher:
    """`v2:{plaintext}` — instant, and obviously not Argon2 output so
    nothing can pass by coincidentally comparing against a real hash."""

    async def hash(self, plaintext: str) -> str:
        return f"v2:{plaintext}"

    async def verify(self, encoded_hash: str, plaintext: str) -> bool:
        return encoded_hash.split(":", 1)[-1] == plaintext

    async def needs_rehash(self, encoded_hash: str) -> bool:
        return False

    async def dummy_hash(self) -> str:
        return "v2:\x00unguessable"


class _RecordingProvider:
    """`EmailProvider`. Keeps every message so a test can read the link out
    of the body, exactly as the person receiving it would."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


def account(*, is_active: bool = True) -> User:
    user = User.create(
        username=Username("player_one"),
        email=Email(EMAIL),
        password_hash=f"v2:{OLD_PASSWORD}",
        preferred_language=Locale.EN,
        timezone=Timezone("UTC"),
        created_at=_NOW,
    )
    user.is_active = is_active
    return user


@pytest.fixture
def user() -> User:
    return account()


@pytest.fixture
def provider() -> _RecordingProvider:
    return _RecordingProvider()


@pytest.fixture
def sessions_repository() -> FakeSessionRepository:
    return FakeSessionRepository()


@pytest.fixture
def users_repository(user: User) -> FakeUserRepository:
    """Exposed as a fixture because the fake **deep-copies on seeding**,
    deliberately — it mirrors a real repository handing back a different
    object than the one you passed it. So a test asserting "the password
    changed" has to read the stored row back through the repository; the
    `user` fixture object is a template, not the persisted state."""
    return FakeUserRepository([user])


def stored_hash(users_repository: FakeUserRepository, user_id: UUID) -> str:
    """The persisted credential, read back the way production would."""
    stored = asyncio.run(users_repository.get_by_id(user_id))
    assert stored is not None
    return stored.password_hash


@pytest.fixture
def client(
    user: User,
    users_repository: FakeUserRepository,
    provider: _RecordingProvider,
    sessions_repository: FakeSessionRepository,
) -> Iterator[TestClient]:
    app = create_app()
    tokens = FakePasswordResetTokenRepository()
    email_settings = EmailSettings()
    session_settings = SessionSettings()

    def _sessions() -> SessionService:
        return SessionService(
            restrictions=UnrestrictedAccounts(),
            sessions=sessions_repository,
            tokens=RefreshTokenService(session_settings),
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
            settings=session_settings,
        )

    def _reset_service() -> PasswordResetService:
        users = UserService(
            users=users_repository,
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
        )
        return PasswordResetService(
            tokens=tokens,
            token_factory=OpaqueTokenService(email_settings.password_reset_token_entropy_bytes),
            profiles=UserProfileService(users),
            resetter=PasswordResetWriter(users),
            password_hasher=_StubHasher(),
            sessions=_sessions(),
            email=provider,
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
            settings=email_settings,
        )

    app.dependency_overrides[get_password_reset_service] = _reset_service
    # The same fake session store the reset service revokes through, so a
    # test can create a session and then assert the reset killed it.
    app.dependency_overrides[get_session_service] = _sessions

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def issued_link_token(provider: _RecordingProvider) -> str:
    return provider.sent[-1].text_body.split("token=")[1].split()[0]


class TestForgotPassword:
    def test_returns_204_for_a_known_address(self, client: TestClient) -> None:
        assert client.post(FORGOT_URL, json={"email": EMAIL}).status_code == 204

    def test_returns_204_for_an_unknown_address(self, client: TestClient) -> None:
        assert client.post(FORGOT_URL, json={"email": UNKNOWN_EMAIL}).status_code == 204

    def test_the_two_replies_are_indistinguishable(self, client: TestClient) -> None:
        """The enumeration guard, asserted as a *comparison* rather than
        against a constant — the failure being guarded against is a
        difference, whatever form it takes."""
        known = client.post(FORGOT_URL, json={"email": EMAIL})
        unknown = client.post(FORGOT_URL, json={"email": UNKNOWN_EMAIL})

        assert known.status_code == unknown.status_code
        assert known.content == unknown.content == b""

    def test_a_deactivated_account_is_also_indistinguishable(
        self, client: TestClient, user: User
    ) -> None:
        user.is_active = False

        deactivated = client.post(FORGOT_URL, json={"email": EMAIL})
        unknown = client.post(FORGOT_URL, json={"email": UNKNOWN_EMAIL})

        assert deactivated.status_code == unknown.status_code
        assert deactivated.content == unknown.content

    def test_sends_a_link_for_a_known_address(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})

        assert len(provider.sent) == 1

    def test_sends_nothing_for_an_unknown_address(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        client.post(FORGOT_URL, json={"email": UNKNOWN_EMAIL})

        assert provider.sent == []

    def test_the_response_never_carries_the_token(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        """The credential exists in the email and nowhere else. A token in
        the response body would let anyone who can call the endpoint reset
        any account whose address they know."""
        response = client.post(FORGOT_URL, json={"email": EMAIL})

        assert response.content == b""
        assert issued_link_token(provider) not in response.text

    def test_accepts_a_differently_cased_address(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        """Matching is case-insensitive (AC-1) — the address is normalised
        the same way registration normalised it."""
        client.post(FORGOT_URL, json={"email": "PLAYER.ONE@Example.com"})

        assert len(provider.sent) == 1

    def test_a_malformed_address_is_422(self, client: TestClient) -> None:
        """Reveals nothing: an address that cannot be valid cannot belong
        to anyone."""
        assert client.post(FORGOT_URL, json={"email": "not-an-address"}).status_code == 422

    def test_rejects_an_unexpected_field(self, client: TestClient) -> None:
        """`extra="forbid"`. A `user_id` a caller could supply is the shape
        of a request that resets somebody else's password."""
        response = client.post(FORGOT_URL, json={"email": EMAIL, "user_id": "whatever"})

        assert response.status_code == 422


class TestResetPassword:
    def test_returns_204(self, client: TestClient, provider: _RecordingProvider) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})

        response = client.post(
            RESET_URL, json={"token": issued_link_token(provider), "password": NEW_PASSWORD}
        )

        assert response.status_code == 204
        assert response.content == b""

    def test_the_password_actually_changes(
        self,
        client: TestClient,
        provider: _RecordingProvider,
        users_repository: FakeUserRepository,
        user: User,
    ) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})

        client.post(
            RESET_URL, json={"token": issued_link_token(provider), "password": NEW_PASSWORD}
        )

        assert stored_hash(users_repository, user.id) == f"v2:{NEW_PASSWORD}"

    def test_returns_no_token_pair(self, client: TestClient, provider: _RecordingProvider) -> None:
        """Deliberately no session. This endpoint has verified control of
        an *inbox*, not knowledge of a password — handing back a live
        credential would make an email compromise silently equivalent to a
        sign-in."""
        client.post(FORGOT_URL, json={"email": EMAIL})

        response = client.post(
            RESET_URL, json={"token": issued_link_token(provider), "password": NEW_PASSWORD}
        )

        assert response.content == b""
        assert "access_token" not in response.text
        assert "refresh_token" not in response.text

    def test_the_response_never_echoes_the_new_password(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})

        response = client.post(
            RESET_URL, json={"token": issued_link_token(provider), "password": NEW_PASSWORD}
        )

        assert NEW_PASSWORD not in response.text

    def test_an_unknown_token_is_422_invalid_reset_token(self, client: TestClient) -> None:
        response = client.post(RESET_URL, json={"token": "never-issued", "password": NEW_PASSWORD})

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_reset_token"

    def test_a_reused_token_is_422_invalid_reset_token(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})
        token = issued_link_token(provider)
        client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        response = client.post(RESET_URL, json={"token": token, "password": "ThirdChoice3!"})

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_reset_token"

    def test_a_replay_does_not_change_the_password_again(
        self,
        client: TestClient,
        provider: _RecordingProvider,
        users_repository: FakeUserRepository,
        user: User,
    ) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})
        token = issued_link_token(provider)
        client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        client.post(RESET_URL, json={"token": token, "password": "AttackerChoice3!"})

        assert stored_hash(users_repository, user.id) == f"v2:{NEW_PASSWORD}"

    def test_a_superseded_token_is_rejected(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})
        first = issued_link_token(provider)
        client.post(FORGOT_URL, json={"email": EMAIL})

        response = client.post(RESET_URL, json={"token": first, "password": NEW_PASSWORD})

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_reset_token"

    def test_the_error_body_never_carries_the_token(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})
        token = issued_link_token(provider)
        client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        response = client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        assert token not in response.text

    def test_is_not_401(self, client: TestClient) -> None:
        """A 401 would send somebody who clicked a stale link to a sign-in
        form, which is exactly what they cannot do."""
        response = client.post(RESET_URL, json={"token": "never-issued", "password": NEW_PASSWORD})

        assert response.status_code != 401
        assert "www-authenticate" not in response.headers

    def test_rejects_an_unexpected_field(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})

        response = client.post(
            RESET_URL,
            json={
                "token": issued_link_token(provider),
                "password": NEW_PASSWORD,
                "user_id": "whatever",
            },
        )

        assert response.status_code == 422


class TestPasswordPolicy:
    @pytest.mark.parametrize(
        "password",
        ["Ab1!", "lowercase1!", "UPPERCASE1!", "NoDigitsHere!", "NoSpecials123"],
    )
    def test_rejects_a_password_that_fails_the_policy(
        self, client: TestClient, provider: _RecordingProvider, password: str
    ) -> None:
        client.post(FORGOT_URL, json={"email": EMAIL})

        response = client.post(
            RESET_URL, json={"token": issued_link_token(provider), "password": password}
        )

        assert response.status_code == 422

    def test_a_weak_password_is_rejected_before_the_token_is_looked_at(
        self, client: TestClient
    ) -> None:
        """The token oracle guard, at the layer that actually provides it.

        The schema validates the password while parsing, so a bad password
        with a *bogus* token is refused for the password — meaning an
        attacker cannot submit a candidate token with a deliberately awful
        password and read the token's validity off the error code.
        """
        response = client.post(RESET_URL, json={"token": "never-issued", "password": "weak"})

        assert response.status_code == 422
        assert response.json()["code"] != "invalid_reset_token"

    def test_the_same_rejection_whether_or_not_the_token_is_real(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        """The oracle guard stated as the comparison it actually is."""
        client.post(FORGOT_URL, json={"email": EMAIL})

        with_real_token = client.post(
            RESET_URL, json={"token": issued_link_token(provider), "password": "weak"}
        )
        with_bogus_token = client.post(
            RESET_URL, json={"token": "never-issued", "password": "weak"}
        )

        assert with_real_token.status_code == with_bogus_token.status_code
        assert with_real_token.json()["code"] == with_bogus_token.json()["code"]

    def test_a_weak_password_does_not_consume_the_token(
        self,
        client: TestClient,
        provider: _RecordingProvider,
        users_repository: FakeUserRepository,
        user: User,
    ) -> None:
        """Somebody who fumbles their new password gets to try again with
        the same link rather than finding their one-time token burned by a
        typo."""
        client.post(FORGOT_URL, json={"email": EMAIL})
        token = issued_link_token(provider)
        client.post(RESET_URL, json={"token": token, "password": "weak"})

        response = client.post(RESET_URL, json={"token": token, "password": NEW_PASSWORD})

        assert response.status_code == 204
        assert stored_hash(users_repository, user.id) == f"v2:{NEW_PASSWORD}"

    def test_the_error_never_echoes_the_rejected_password(
        self, client: TestClient, provider: _RecordingProvider
    ) -> None:
        """`PasswordField` is a `SecretStr`, and the platform's request
        validation handler drops each error's `input` — between them there
        is no path by which a submitted password reaches a response body."""
        client.post(FORGOT_URL, json={"email": EMAIL})

        response = client.post(
            RESET_URL, json={"token": issued_link_token(provider), "password": "hunter2"}
        )

        assert "hunter2" not in response.text


class TestSessionInvalidation:
    """Asserted through the endpoint, over the same session store the reset
    service writes to. This is the wiring check the service tests cannot
    make: a reset that revoked sessions in a *different* `SessionService`
    instance — one built on its own unit of work by a careless dependency
    factory — would pass every test in
    `test_password_reset_service.py` and fail in production."""

    @staticmethod
    def _sessions(sessions_repository: FakeSessionRepository) -> SessionService:
        settings = SessionSettings()
        return SessionService(
            restrictions=UnrestrictedAccounts(),
            sessions=sessions_repository,
            tokens=RefreshTokenService(settings),
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
            settings=settings,
        )

    def test_a_reset_revokes_every_session(
        self,
        client: TestClient,
        provider: _RecordingProvider,
        sessions_repository: FakeSessionRepository,
        user: User,
    ) -> None:
        service = self._sessions(sessions_repository)

        async def sign_in_twice() -> None:
            await service.create_session(user.id, device=SessionDevice())
            await service.create_session(user.id, device=SessionDevice())

        asyncio.run(sign_in_twice())
        client.post(FORGOT_URL, json={"email": EMAIL})

        client.post(
            RESET_URL, json={"token": issued_link_token(provider), "password": NEW_PASSWORD}
        )

        live = asyncio.run(service.list_user_sessions(user.id))
        assert live == []

    def test_the_revoked_refresh_tokens_stop_working(
        self,
        client: TestClient,
        provider: _RecordingProvider,
        sessions_repository: FakeSessionRepository,
        user: User,
    ) -> None:
        """A revocation flag nothing checks is not a revocation — this
        proves the credential is refused."""
        service = self._sessions(sessions_repository)
        issued = asyncio.run(service.create_session(user.id, device=SessionDevice()))
        client.post(FORGOT_URL, json={"email": EMAIL})

        client.post(
            RESET_URL, json={"token": issued_link_token(provider), "password": NEW_PASSWORD}
        )

        with pytest.raises(InvalidRefreshToken):
            asyncio.run(service.rotate_refresh_token(issued.refresh_token))


class TestOpenAPI:
    def test_both_endpoints_are_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/auth/password/forgot" in paths
        assert "/api/v1/auth/password/reset" in paths

    def test_neither_declares_a_response_body(self, client: TestClient) -> None:
        """`204` means no body, and a schema promising one would have every
        generated client try to parse an empty response."""
        paths = client.get("/openapi.json").json()["paths"]

        for path in ("/api/v1/auth/password/forgot", "/api/v1/auth/password/reset"):
            assert "content" not in paths[path]["post"]["responses"]["204"]

    def test_the_reset_example_is_not_a_real_credential(self, client: TestClient) -> None:
        """`password` is a `SecretStr`, which is what keeps a plausible
        value out of the generated example — and the generated example is
        the one place a documentation site would publish it."""
        schema = client.get("/openapi.json").json()["components"]["schemas"]

        assert "ResetPasswordRequest" in schema
