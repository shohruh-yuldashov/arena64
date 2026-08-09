"""`POST /auth/login` — the HTTP surface.

In `tests/unit/` per services.md §1's definition ("no I/O, fakes only"):
the database is a `FakeUserRepository` and the hasher is a stub, both
swapped in through FastAPI's `dependency_overrides`. Routing, middleware,
the response envelope and every exception handler are the production ones
— which is the point, since half of what this file asserts is that the
right status and the right wire code come out the other end without
`auth` registering a handler of its own.

`tests/contract/test_login_endpoint.py` runs the same endpoint against
real PostgreSQL and real Argon2id.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest
from fastapi.testclient import TestClient

from app.app_factory import create_app
from app.config.settings import SessionSettings
from app.core.enums import Locale
from app.modules.auth.application.services import (
    AuthenticationService,
    RefreshTokenService,
    SessionService,
)
from app.modules.auth.presentation.dependencies import (
    get_authentication_service,
    get_password_hasher,
    get_session_service,
)
from app.modules.users.application.services import UserService
from app.modules.users.application.services.user_credential_service import UserCredentialService
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import Email, Timezone, Username
from tests.fakes.moderation import UnrestrictedAccounts
from tests.fakes.session_repository import FakeSessionRepository
from tests.fakes.user_repository import FakeUserRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LOGIN_URL = "/api/v1/auth/login"
EMAIL = "player.one@example.com"
PASSWORD = "CorrectHorse1!"
WRONG_PASSWORD = "WrongHorse9?"
VALID_BODY = {"email": EMAIL, "password": PASSWORD}


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
        return not encoded_hash.startswith("v2:")

    async def dummy_hash(self) -> str:
        return "v2:\x00unguessable"


def account(*, is_active: bool = True, locked_until: datetime | None = None) -> User:
    user = User.create(
        username=Username("player_one"),
        email=Email(EMAIL),
        password_hash=f"v2:{PASSWORD}",
        preferred_language=Locale.EN,
        timezone=Timezone("UTC"),
        created_at=_NOW,
    )
    user.is_active = is_active
    user.locked_until = locked_until
    return user


@pytest.fixture
def user() -> User:
    return account()


@pytest.fixture
def client(user: User) -> Iterator[TestClient]:
    app = create_app()

    def _authentication_service() -> AuthenticationService:
        users = UserService(
            users=FakeUserRepository([user]),
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
        )
        return AuthenticationService(
            restrictions=UnrestrictedAccounts(),
            credentials=UserCredentialService(users),
            password_hasher=_StubHasher(),
            clock=_FixedClock(),
        )

    def _session_service() -> SessionService:
        return SessionService(
            restrictions=UnrestrictedAccounts(),
            sessions=FakeSessionRepository(),
            tokens=RefreshTokenService(SessionSettings()),
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
            settings=SessionSettings(),
        )

    app.dependency_overrides[get_authentication_service] = _authentication_service
    app.dependency_overrides[get_password_hasher] = _StubHasher
    # A64-011.5: login now issues a token pair, so the two services behind
    # that are part of this endpoint's graph. The access token service is
    # the real one — HMAC is microseconds and a stub would prove nothing.
    app.dependency_overrides[get_session_service] = _session_service

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestSuccessfulLogin:
    """A64-011.2 asserted this endpoint returned the *account* and no
    token. A64-011.5 changed that contract deliberately, so these
    assertions moved with it — the credential-secrecy ones below are the
    part that did not change, and must not."""

    def test_returns_200(self, client: TestClient) -> None:
        """Not 201. A session row is created, but what the caller asked
        for is a credential, not a resource with a URL."""
        assert client.post(LOGIN_URL, json=VALID_BODY).status_code == 200

    def test_returns_a_token_pair_in_the_platform_envelope(self, client: TestClient) -> None:
        body = client.post(LOGIN_URL, json=VALID_BODY).json()

        assert body["data"]["access_token"]
        assert body["data"]["refresh_token"]
        assert body["data"]["token_type"] == "Bearer"
        assert body["data"]["expires_in"] == 900
        assert body["meta"]["request_id"]
        assert body["meta"]["correlation_id"]

    def test_the_response_carries_no_password_hash(self, client: TestClient) -> None:
        """Asserted against the raw response text, not just the parsed
        `data` — a hash leaking through `meta`, an error field or a header
        would be just as much of a disclosure."""
        response = client.post(LOGIN_URL, json=VALID_BODY)

        assert "password" not in response.text
        assert "v2:" not in response.text

    def test_the_response_carries_no_profile(self, client: TestClient) -> None:
        """The token pair and nothing else. Returning the account here
        would put an email in every sign-in response body for no caller
        that asked — `GET /auth/me` is the endpoint for that."""
        body = client.post(LOGIN_URL, json=VALID_BODY).json()

        assert set(body["data"]) == {
            "access_token",
            "refresh_token",
            "token_type",
            "expires_in",
        }
        assert EMAIL not in body["data"].values()

    def test_no_cookie_is_set(self, client: TestClient) -> None:
        """The refresh token is returned in the body, so the endpoint works
        for a native client as well as a browser. Storing it in an
        `HttpOnly` cookie is the SPA's decision, not this endpoint's."""
        assert client.post(LOGIN_URL, json=VALID_BODY).cookies == {}

    def test_accepts_a_differently_cased_address(self, client: TestClient) -> None:
        response = client.post(LOGIN_URL, json={**VALID_BODY, "email": "PLAYER.ONE@Example.com"})

        assert response.status_code == 200
        assert response.json()["data"]["access_token"]


class TestFailedLogin:
    def test_a_wrong_password_returns_401(self, client: TestClient) -> None:
        response = client.post(LOGIN_URL, json={**VALID_BODY, "password": WRONG_PASSWORD})

        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"

    def test_an_unknown_address_returns_401(self, client: TestClient) -> None:
        response = client.post(LOGIN_URL, json={**VALID_BODY, "email": "nobody@example.com"})

        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"

    def test_the_two_responses_are_byte_identical(self, client: TestClient) -> None:
        """Everything except the per-request correlation identifiers, which
        differ on any two requests. Anything else differing — a status, a
        code, a message, a header — is an account-enumeration oracle.
        """
        unknown = client.post(LOGIN_URL, json={**VALID_BODY, "email": "nobody@example.com"})
        wrong = client.post(LOGIN_URL, json={**VALID_BODY, "password": WRONG_PASSWORD})

        assert unknown.status_code == wrong.status_code
        assert _without_request_ids(unknown.json()) == _without_request_ids(wrong.json())

    def test_the_error_body_never_echoes_the_address(self, client: TestClient) -> None:
        response = client.post(LOGIN_URL, json={**VALID_BODY, "password": WRONG_PASSWORD})

        assert EMAIL not in response.text

    def test_the_error_body_never_echoes_the_password(self, client: TestClient) -> None:
        """FastAPI's own validation errors echo the submitted `input` back
        by default; the platform's handler strips it (A64-010), and
        `SecretStr` covers the rest. This is the assertion that keeps both
        honest on the one endpoint where it matters most."""
        response = client.post(LOGIN_URL, json={**VALID_BODY, "password": WRONG_PASSWORD})

        assert WRONG_PASSWORD not in response.text

    def test_a_bare_bearer_challenge_is_sent(self, client: TestClient) -> None:
        """RFC 9110 §11.6.1: a 401 must name an applicable scheme.

        A64-011.2 asserted this header was *absent*, because the platform
        had no scheme and advertising `Bearer` before bearer tokens
        existed would have named something no endpoint accepted. That test
        existed to force the decision to be revisited when tokens arrived;
        A64-011.3 is that task, and this is the revisit.

        Bare `Bearer`, with no RFC 6750 `error` parameter: those describe
        what was wrong with a *presented* token, and a failed sign-in
        presented none.
        """
        response = client.post(LOGIN_URL, json={**VALID_BODY, "password": WRONG_PASSWORD})

        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_the_challenge_does_not_describe_a_token(self, client: TestClient) -> None:
        """The header must not become the oracle the body refuses to be —
        an `error_description` here would say more about the failure than
        the generic `invalid_credentials` message does."""
        response = client.post(LOGIN_URL, json={**VALID_BODY, "password": WRONG_PASSWORD})

        assert "error=" not in response.headers["WWW-Authenticate"]


class TestInactiveAccount:
    @pytest.fixture
    def user(self) -> User:
        return account(is_active=False)

    def test_returns_403_with_its_own_code(self, client: TestClient) -> None:
        """403, not 401: the caller *did* prove who they are, and a client
        that saw 401 would be right to prompt for the password again —
        exactly the wrong instruction for someone whose password was
        correct."""
        response = client.post(LOGIN_URL, json=VALID_BODY)

        assert response.status_code == 403
        assert response.json()["code"] == "inactive_account"

    def test_a_wrong_password_still_returns_the_generic_401(self, client: TestClient) -> None:
        """Which is what stops `inactive_account` from being the
        enumeration oracle it looks like."""
        response = client.post(LOGIN_URL, json={**VALID_BODY, "password": WRONG_PASSWORD})

        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"


class TestLockedAccount:
    @pytest.fixture
    def user(self) -> User:
        return account(locked_until=_NOW + timedelta(minutes=15))

    def test_returns_403_with_its_own_code(self, client: TestClient) -> None:
        """A separate code from `inactive_account` because the client's
        correct response differs: a lock lapses on its own ("try again
        later"), a deactivation does not ("contact support")."""
        response = client.post(LOGIN_URL, json=VALID_BODY)

        assert response.status_code == 403
        assert response.json()["code"] == "account_locked"

    def test_a_wrong_password_still_returns_the_generic_401(self, client: TestClient) -> None:
        response = client.post(LOGIN_URL, json={**VALID_BODY, "password": WRONG_PASSWORD})

        assert response.status_code == 401


class TestRequestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"password": PASSWORD}, id="missing-email"),
            pytest.param({"email": EMAIL}, id="missing-password"),
            pytest.param({}, id="empty"),
            pytest.param({**VALID_BODY, "password": ""}, id="blank-password"),
        ],
    )
    def test_an_incomplete_body_is_a_422(self, client: TestClient, body: dict[str, str]) -> None:
        assert client.post(LOGIN_URL, json=body).status_code == 422

    def test_a_malformed_address_is_a_422(self, client: TestClient) -> None:
        """The right feedback for a form, and no disclosure: an address
        that cannot be valid cannot belong to anyone. Non-HTTP callers get
        the generic 401 instead — see `AuthenticationService`."""
        response = client.post(LOGIN_URL, json={**VALID_BODY, "email": "not-an-email"})

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_email"

    def test_unknown_fields_are_rejected(self, client: TestClient) -> None:
        """`extra="forbid"` from `BaseRequestDTO`. A client sending
        `is_active` alongside its credentials is told no, rather than
        having it silently ignored."""
        response = client.post(LOGIN_URL, json={**VALID_BODY, "is_active": True})

        assert response.status_code == 422

    def test_a_validation_failure_never_echoes_the_password(self, client: TestClient) -> None:
        """The path that would leak it: FastAPI's native error payload
        includes each error's `input`. Sending a bad *email* with a good
        password is what puts the password in a rejected body."""
        response = client.post(LOGIN_URL, json={"email": "not-an-email", "password": PASSWORD})

        assert PASSWORD not in response.text

    def test_an_oversized_password_is_rejected_before_hashing(self, client: TestClient) -> None:
        """A resource guard, not a policy: Argon2id is deliberately
        memory-hungry, and an unauthenticated endpoint must not let a
        multi-megabyte body reach it."""
        response = client.post(LOGIN_URL, json={**VALID_BODY, "password": "a" * 100_000})

        assert response.status_code == 422


_PER_REQUEST_KEYS = {"request_id", "correlation_id"}


def _without_request_ids(body: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in body.items() if key not in _PER_REQUEST_KEYS}
