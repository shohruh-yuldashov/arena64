"""The authentication dependencies, exercised through a real FastAPI app.

Not by calling `get_current_user()` directly — that would prove the
function works and nothing about the part that actually breaks. What is
under test here is the *integration*: that `HTTPBearer` parses the header
the way this code assumes, that a rejection routes through
`app/api/exception_handlers.py` into the platform envelope instead of
FastAPI's native `{"detail": ...}`, that the status is 401 and not 403,
and that the `WWW-Authenticate` challenge is right. Every one of those is
a seam between this code and the framework.

Routes are mounted on a throwaway app rather than on the real router,
because A64-011.3 adds no endpoints — the infrastructure exists before
anything is protected by it, and these routes are the test's own.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.exception_handlers import register_exception_handlers
from app.app_factory import create_app
from app.config.settings import JWTSettings, get_settings
from app.core.clock import SystemClock
from app.modules.auth.application.services import AccessTokenService, TokenValidator
from app.modules.auth.domain.tokens import TokenType
from app.modules.auth.infrastructure import JwtTokenProvider
from app.modules.auth.presentation.dependencies import (
    CurrentUser,
    OptionalCurrentUser,
    RequireAuthentication,
    get_access_token_service,
    get_token_validator,
)
from app.modules.auth.public import AuthenticatedUser

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
USER_ID = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")
SIGNING_KEY = "a-signing-key-comfortably-over-the-configured-minimum"


class MovableClock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def settings() -> JWTSettings:
    return JWTSettings(secret_key=SecretStr(SIGNING_KEY))


@pytest.fixture
def provider(settings: JWTSettings, clock: MovableClock) -> JwtTokenProvider:
    return JwtTokenProvider(settings, clock)


@pytest.fixture
def token(provider: JwtTokenProvider) -> str:
    issued, _ = provider.issue(
        subject=str(USER_ID), token_type=TokenType.ACCESS, lifetime_seconds=900
    )
    return issued


@pytest.fixture
def client(provider: JwtTokenProvider) -> Iterator[TestClient]:
    """A minimal app carrying the platform's own exception handlers, so a
    401 here goes through exactly the machinery a real route's would."""
    app = FastAPI()
    register_exception_handlers(app)

    router = APIRouter()

    @router.get("/protected")
    async def protected(user: CurrentUser) -> dict[str, str]:
        return {"user_id": str(user.id), "token_id": str(user.token_id)}

    @router.get("/optional")
    async def optional(user: OptionalCurrentUser) -> dict[str, str | None]:
        return {"user_id": str(user.id) if user else None}

    guarded = APIRouter(prefix="/guarded", dependencies=[Depends(RequireAuthentication())])

    @guarded.get("/resource")
    async def guarded_resource() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    app.include_router(guarded)

    app.dependency_overrides[get_token_validator] = lambda: TokenValidator(tokens=provider)

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def forge(*, key: str = SIGNING_KEY, algorithm: str = "HS256", **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "sub": str(USER_ID),
        "jti": str(uuid4()),
        "type": "access",
        "iat": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 900,
        "iss": "arena64",
        "aud": "arena64-api",
    }
    payload.update(overrides)
    payload = {name: value for name, value in payload.items() if value is not None}
    return jwt.encode(payload, key=key, algorithm=algorithm)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestGetCurrentUser:
    def test_a_valid_token_authenticates(self, client: TestClient, token: str) -> None:
        response = client.get("/protected", headers=auth(token))

        assert response.status_code == 200
        assert response.json()["user_id"] == str(USER_ID)

    def test_the_identity_comes_from_the_token_claims(
        self, client: TestClient, provider: JwtTokenProvider, token: str
    ) -> None:
        """No database was read to produce it — `AuthenticatedUser` is
        exactly what the signature proved, which is the whole point of a
        stateless credential."""
        claims = provider.decode(token, expected_type=TokenType.ACCESS)
        body = client.get("/protected", headers=auth(token)).json()

        assert body["token_id"] == str(claims.token_id)

    def test_a_missing_header_is_401(self, client: TestClient) -> None:
        response = client.get("/protected")

        assert response.status_code == 401
        assert response.json()["code"] == "authentication_required"

    def test_the_rejection_uses_the_platform_envelope(self, client: TestClient) -> None:
        """`HTTPBearer(auto_error=False)` exists for this: with
        `auto_error=True` FastAPI raises its own `HTTPException` and the
        body comes back as `{"detail": ...}`, which
        `apps/web/src/services/error-parser.ts` cannot read — the same
        envelope break A64-010 had to fix for request validation."""
        body = client.get("/protected").json()

        assert set(body) == {"code", "message", "request_id", "correlation_id"}

    @pytest.mark.parametrize(
        "header",
        [
            pytest.param({"Authorization": "Bearer"}, id="scheme-with-no-token"),
            pytest.param({"Authorization": "Basic dXNlcjpwYXNz"}, id="wrong-scheme"),
            pytest.param({"Authorization": "token abc"}, id="unknown-scheme"),
            pytest.param({"Authorization": ""}, id="empty"),
        ],
    )
    def test_a_non_bearer_header_is_treated_as_no_credential(
        self, client: TestClient, header: dict[str, str]
    ) -> None:
        response = client.get("/protected", headers=header)

        assert response.status_code == 401
        assert response.json()["code"] == "authentication_required"

    def test_an_expired_token_is_401_with_its_own_code(
        self, client: TestClient, token: str, clock: MovableClock
    ) -> None:
        """The one token failure a client must handle differently: refresh
        and retry (A64-011.4), rather than sign the user out."""
        clock.instant = NOW + timedelta(seconds=901)

        response = client.get("/protected", headers=auth(token))

        assert response.status_code == 401
        assert response.json()["code"] == "expired_token"

    @pytest.mark.parametrize(
        ("label", "token_factory"),
        [
            pytest.param("wrong-key", lambda: forge(key="x" * 48), id="wrong-signature"),
            pytest.param("alg-none", lambda: forge(key="", algorithm="none"), id="alg-none"),
            pytest.param("wrong-iss", lambda: forge(iss="elsewhere"), id="wrong-issuer"),
            pytest.param("wrong-aud", lambda: forge(aud="elsewhere"), id="wrong-audience"),
            pytest.param("no-exp", lambda: forge(exp=None), id="missing-exp"),
            pytest.param("bad-sub", lambda: forge(sub="root"), id="unparsable-subject"),
            pytest.param("garbage", lambda: "not.a.token", id="malformed"),
        ],
    )
    def test_an_untrustworthy_token_is_401_invalid_token(
        self, client: TestClient, label: str, token_factory: Any
    ) -> None:
        """All seven collapse to one code and one message on purpose —
        anything finer is a step-by-step oracle for shaping a forgery."""
        response = client.get("/protected", headers=auth(token_factory()))

        assert response.status_code == 401, label
        assert response.json()["code"] == "invalid_token", label

    def test_every_invalid_token_produces_the_same_message(self, client: TestClient) -> None:
        messages = {
            client.get("/protected", headers=auth(candidate)).json()["message"]
            for candidate in (
                forge(key="x" * 48),
                forge(iss="elsewhere"),
                forge(aud="elsewhere"),
                forge(sub="root"),
                "not.a.token",
            )
        }

        assert len(messages) == 1

    def test_401_and_not_403(self, client: TestClient) -> None:
        """403 would tell a client "I know who you are and you may not do
        this", which sends an unauthenticated caller somewhere other than
        a sign-in form."""
        assert client.get("/protected").status_code == 401


class TestWwwAuthenticateChallenge:
    def test_a_missing_credential_gets_a_bare_challenge(self, client: TestClient) -> None:
        """RFC 9110 §11.6.1 — the header A64-011.2 deferred until there
        was a scheme to name."""
        response = client.get("/protected")

        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_a_rejected_token_gets_rfc_6750_s_error_parameter(self, client: TestClient) -> None:
        """What tells a client library to stop retrying with the
        credential it holds."""
        response = client.get("/protected", headers=auth(forge(key="x" * 48)))

        assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]

    def test_an_expired_token_also_reports_invalid_token(
        self, client: TestClient, token: str, clock: MovableClock
    ) -> None:
        """RFC 6750 defines exactly three error codes and `expired_token`
        is not one of them. The platform's own `expired_token` is in the
        body, where a client that knows this API can act on it; the header
        stays within the vocabulary a generic HTTP client can parse."""
        clock.instant = NOW + timedelta(days=1)

        challenge = client.get("/protected", headers=auth(token)).headers["WWW-Authenticate"]

        assert 'error="invalid_token"' in challenge

    def test_the_challenge_never_says_which_check_failed(self, client: TestClient) -> None:
        challenges = {
            client.get("/protected", headers=auth(candidate)).headers["WWW-Authenticate"]
            for candidate in (
                forge(key="x" * 48),
                forge(iss="elsewhere"),
                forge(aud="elsewhere"),
            )
        }

        assert len(challenges) == 1

    def test_the_challenge_is_absent_on_success(self, client: TestClient, token: str) -> None:
        response = client.get("/protected", headers=auth(token))

        assert "WWW-Authenticate" not in response.headers


class TestGetCurrentUserOptional:
    def test_returns_none_without_a_credential(self, client: TestClient) -> None:
        response = client.get("/optional")

        assert response.status_code == 200
        assert response.json()["user_id"] is None

    def test_returns_the_identity_with_a_valid_one(self, client: TestClient, token: str) -> None:
        response = client.get("/optional", headers=auth(token))

        assert response.json()["user_id"] == str(USER_ID)

    @pytest.mark.parametrize(
        "candidate",
        [
            pytest.param(forge(key="x" * 48), id="forged"),
            pytest.param(forge(aud="elsewhere"), id="wrong-audience"),
            pytest.param("garbage", id="malformed"),
        ],
    )
    def test_an_invalid_token_still_raises_rather_than_degrading_to_anonymous(
        self, client: TestClient, candidate: str
    ) -> None:
        """ "Optional" means *absent*, never *rejected*. Silently treating a
        forged token as "not signed in" serves the signed-out view to
        someone whose session just broke, with nothing in the logs to
        explain it — and makes a tampered token indistinguishable from no
        token."""
        assert client.get("/optional", headers=auth(candidate)).status_code == 401

    def test_an_expired_token_also_raises(
        self, client: TestClient, token: str, clock: MovableClock
    ) -> None:
        clock.instant = NOW + timedelta(days=1)

        response = client.get("/optional", headers=auth(token))

        assert response.status_code == 401
        assert response.json()["code"] == "expired_token"


class TestRequireAuthentication:
    def test_a_guarded_router_admits_a_valid_token(self, client: TestClient, token: str) -> None:
        assert client.get("/guarded/resource", headers=auth(token)).status_code == 200

    def test_a_guarded_router_rejects_an_anonymous_request(self, client: TestClient) -> None:
        """The reason it exists: a router protecting twenty endpoints
        declares the guard once, rather than threading an unused parameter
        through twenty signatures — where a later edit deletes it as dead
        and silently unprotects the route."""
        response = client.get("/guarded/resource")

        assert response.status_code == 401
        assert response.json()["code"] == "authentication_required"

    def test_a_guarded_router_rejects_a_forged_token(self, client: TestClient) -> None:
        response = client.get("/guarded/resource", headers=auth(forge(key="x" * 48)))

        assert response.status_code == 401

    def test_the_handler_needs_no_parameter(self, client: TestClient, token: str) -> None:
        """The guarded route's handler takes nothing at all, and is still
        protected."""
        assert client.get("/guarded/resource", headers=auth(token)).json() == {"ok": True}


class TestOpenApiIntegration:
    def test_the_bearer_scheme_is_advertised(self, client: TestClient) -> None:
        """What puts the padlock in the docs and makes "Authorize" work —
        the practical payoff of using `HTTPBearer` rather than reading the
        header by hand."""
        schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]

        assert any(
            scheme.get("type") == "http" and scheme.get("scheme") == "bearer"
            for scheme in schemes.values()
        )

    def test_the_principal_is_not_a_response_model_anywhere(self, client: TestClient) -> None:
        """`AuthenticatedUser` is a plain dataclass precisely so it cannot
        become one by accident — a client that just presented a token
        learns nothing from the platform's reading of it."""
        schema = client.get("/openapi.json").json()

        assert "AuthenticatedUser" not in schema.get("components", {}).get("schemas", {})


class TestNoTokenLeakage:
    def test_a_rejected_token_is_not_echoed_in_the_response(self, client: TestClient) -> None:
        """A token in an error body is a token in a browser console, a bug
        report and a screenshot — and if the rejection was an expiry, it
        was valid moments ago."""
        candidate = forge(key="x" * 48)

        assert candidate not in client.get("/protected", headers=auth(candidate)).text

    def test_a_valid_token_is_not_echoed_either(self, client: TestClient, token: str) -> None:
        assert token not in client.get("/protected", headers=auth(token)).text


class TestPrincipalShape:
    def test_carries_identity_and_token_facts_only(self) -> None:
        """No username, no email, no roles. Adding any of them here would
        mean either a database read on every request — undoing the point
        of a stateless token — or a copy of mutable data that can be
        wrong."""
        user = AuthenticatedUser(
            id=USER_ID, token_id=uuid4(), issued_at=NOW, expires_at=NOW + timedelta(minutes=15)
        )

        assert set(AuthenticatedUser.__slots__) == {
            "id",
            "token_id",
            "issued_at",
            "expires_at",
        }
        assert user.id == USER_ID

    def test_is_immutable(self) -> None:
        user = AuthenticatedUser(
            id=USER_ID, token_id=uuid4(), issued_at=NOW, expires_at=NOW + timedelta(minutes=15)
        )

        with pytest.raises(AttributeError):
            user.id = uuid4()  # type: ignore[misc]


class TestAccessTokenServiceIsWiredButUnused:
    def test_a64_011_3_adds_no_endpoints(self) -> None:
        """The task's explicit boundary. `AccessTokenService` exists and is
        assembled in `auth.presentation.dependencies`, but `POST
        /auth/login` still returns only the account — issuing the token
        there is A64-011.4's line to add."""
        schema = create_app().openapi()
        login = schema["paths"]["/api/v1/auth/login"]["post"]
        response_schema = login["responses"]["200"]["content"]["application/json"]["schema"]

        assert "token" not in str(response_schema).lower()

    def test_the_service_is_constructible_from_the_composition_root(self) -> None:
        service = get_access_token_service(get_settings(), SystemClock())

        assert isinstance(service, AccessTokenService)
