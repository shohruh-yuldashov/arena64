"""`POST /auth/register` — the HTTP surface.

In `tests/unit/` per services.md §1's definition ("no I/O, fakes only"):
the database is a `FakeUserRepository` and the hasher is a stub, both
swapped in through FastAPI's `dependency_overrides`. Routing, middleware,
the response envelope and every exception handler are the production ones.

`tests/contract/test_register_endpoint.py` runs the same endpoint against
real PostgreSQL and real Argon2id.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest
from fastapi.testclient import TestClient

from app.app_factory import create_app
from app.modules.auth.application.services import RegistrationService
from app.modules.auth.presentation.dependencies import (
    get_password_hasher,
    get_registration_service,
)
from app.modules.users.application.services import UserService
from app.modules.users.application.services.user_account_service import UserAccountService
from app.modules.users.presentation.dependencies import get_user_service
from tests.fakes.user_repository import FakeUserRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
VALID_BODY = {
    "username": "player_one",
    "email": "player.one@example.com",
    "password": "CorrectHorse1!",
}


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
    """Fast and obviously fake — real Argon2id is covered by
    `test_password_hasher.py`, and paying 20ms per HTTP test to re-prove it
    would be waste."""

    async def hash(self, plaintext: str) -> str:
        return f"stub-hash${len(plaintext)}"

    # Unused by registration; present so this still satisfies the
    # `PasswordHasher` protocol after A64-011.2 widened it. Raising rather
    # than returning a plausible value means a route that quietly started
    # verifying would fail here instead of passing against a fiction.
    async def verify(self, encoded_hash: str, plaintext: str) -> bool:
        raise AssertionError("registration never verifies a password")

    async def needs_rehash(self, encoded_hash: str) -> bool:
        raise AssertionError("registration never rehashes")

    async def dummy_hash(self) -> str:
        raise AssertionError("registration has no unknown-account path")


@pytest.fixture
def repository() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def client(repository: FakeUserRepository) -> Iterator[TestClient]:
    app = create_app()

    def _users_service() -> UserService:
        return UserService(users=repository, unit_of_work=_NullUnitOfWork(), clock=_FixedClock())

    def _registration_service() -> RegistrationService:
        return RegistrationService(
            accounts=UserAccountService(_users_service()), password_hasher=_StubHasher()
        )

    app.dependency_overrides[get_registration_service] = _registration_service
    app.dependency_overrides[get_password_hasher] = _StubHasher
    # `GET /users/{id}` is overridden too, onto the *same* repository, so a
    # test can follow the `Location` header a registration returns and
    # actually find the account there.
    app.dependency_overrides[get_user_service] = _users_service

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestSuccessfulRegistration:
    def test_returns_201(self, client: TestClient) -> None:
        assert client.post("/api/v1/auth/register", json=VALID_BODY).status_code == 201

    def test_returns_the_user_in_the_platform_envelope(self, client: TestClient) -> None:
        body = client.post("/api/v1/auth/register", json=VALID_BODY).json()

        assert body["data"]["username"] == "player_one"
        assert body["data"]["email"] == "player.one@example.com"
        assert body["meta"]["request_id"]
        assert body["meta"]["correlation_id"]

    def test_sets_a_location_header_to_the_created_resource(self, client: TestClient) -> None:
        response = client.post("/api/v1/auth/register", json=VALID_BODY)
        user_id = response.json()["data"]["id"]

        assert response.headers["Location"] == f"/api/v1/users/{user_id}"

    def test_the_location_header_is_actually_fetchable(self, client: TestClient) -> None:
        """Asserting the string alone is how a wrong prefix gets enshrined
        — the first version of this test did exactly that, matching a
        `Location` of `/v1/users/...` that would have 404'd. Following it
        is the assertion that cannot be satisfied by a wrong value."""
        created = client.post("/api/v1/auth/register", json=VALID_BODY)

        followed = client.get(created.headers["Location"])

        assert followed.status_code == 200
        assert followed.json()["data"]["id"] == created.json()["data"]["id"]

    def test_the_new_account_is_active_and_unverified(self, client: TestClient) -> None:
        data = client.post("/api/v1/auth/register", json=VALID_BODY).json()["data"]

        assert data["is_active"] is True
        assert data["is_verified"] is False

    def test_accepts_optional_profile_fields(self, client: TestClient) -> None:
        data = client.post(
            "/api/v1/auth/register",
            json=VALID_BODY | {"preferred_language": "uz", "timezone": "Asia/Samarkand"},
        ).json()["data"]

        assert data["preferred_language"] == "uz"
        assert data["timezone"] == "Asia/Samarkand"

    def test_defaults_language_and_timezone(self, client: TestClient) -> None:
        data = client.post("/api/v1/auth/register", json=VALID_BODY).json()["data"]

        assert data["preferred_language"] == "en"
        assert data["timezone"] == "UTC"


class TestNeverExposesTheCredential:
    def test_the_response_contains_neither_password_nor_hash(self, client: TestClient) -> None:
        """The single most important test in this file."""
        response = client.post("/api/v1/auth/register", json=VALID_BODY)

        assert "CorrectHorse1!" not in response.text
        assert "password" not in response.json()["data"]
        assert "password_hash" not in response.json()["data"]
        assert "stub-hash" not in response.text

    @pytest.mark.parametrize(
        "bad_body",
        [
            {**VALID_BODY, "password": "weak"},  # rejected by policy
            {**VALID_BODY, "password": "x"},  # rejected by Pydantic min_length
            {**VALID_BODY, "email": "not-an-email"},  # rejected before password use
            {**VALID_BODY, "username": "ab"},
        ],
    )
    def test_no_error_response_ever_echoes_the_submitted_password(
        self, client: TestClient, bad_body: dict[str, str]
    ) -> None:
        """Covers both error paths: the platform's domain-exception handler
        *and* FastAPI's own request-validation handler, which is why the
        latter drops each error's `input` field (A64-010)."""
        response = client.post("/api/v1/auth/register", json=bad_body)

        assert response.status_code == 422
        assert bad_body["password"] not in response.text

    def test_openapi_response_schema_has_no_password_field(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()["components"]["schemas"]["UserRead"]

        assert "password" not in schema["properties"]
        assert "password_hash" not in schema["properties"]


class TestDuplicateEmail:
    def test_returns_409_with_a_field_specific_code(self, client: TestClient) -> None:
        client.post("/api/v1/auth/register", json=VALID_BODY)

        response = client.post(
            "/api/v1/auth/register", json=VALID_BODY | {"username": "someone_else"}
        )

        assert response.status_code == 409
        # A form needs to know *which* field collided, which a bare
        # `conflict` could not say.
        assert response.json()["code"] == "email_already_exists"

    def test_is_case_insensitive(self, client: TestClient) -> None:
        client.post("/api/v1/auth/register", json=VALID_BODY)

        response = client.post(
            "/api/v1/auth/register",
            json=VALID_BODY | {"username": "someone_else", "email": "Player.One@EXAMPLE.com"},
        )

        assert response.status_code == 409


class TestDuplicateUsername:
    def test_returns_409_with_a_field_specific_code(self, client: TestClient) -> None:
        client.post("/api/v1/auth/register", json=VALID_BODY)

        response = client.post(
            "/api/v1/auth/register", json=VALID_BODY | {"email": "other@example.com"}
        )

        assert response.status_code == 409
        assert response.json()["code"] == "username_already_exists"

    def test_is_case_insensitive(self, client: TestClient) -> None:
        client.post("/api/v1/auth/register", json=VALID_BODY)

        response = client.post(
            "/api/v1/auth/register",
            json=VALID_BODY | {"username": "PLAYER_ONE", "email": "other@example.com"},
        )

        assert response.status_code == 409


class TestInvalidPassword:
    @pytest.mark.parametrize(
        "password",
        ["nouppercase1!", "NOLOWERCASE1!", "NoDigitsHere!", "NoSpecial123x"],
    )
    def test_returns_422_weak_password(self, client: TestClient, password: str) -> None:
        response = client.post("/api/v1/auth/register", json=VALID_BODY | {"password": password})

        assert response.status_code == 422
        assert response.json()["code"] == "weak_password"

    def test_too_short_is_rejected_by_the_schema_before_the_policy(
        self, client: TestClient
    ) -> None:
        # `Field(min_length=...)` fires during parsing, so this is a
        # framework validation error rather than a domain one — still 422,
        # still the platform envelope, and still no password echoed.
        response = client.post("/api/v1/auth/register", json=VALID_BODY | {"password": "Aa1!"})

        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    def test_a_missing_password_is_rejected(self, client: TestClient) -> None:
        body = {k: v for k, v in VALID_BODY.items() if k != "password"}
        response = client.post("/api/v1/auth/register", json=body)

        assert response.status_code == 422
        assert "password" in response.json()["message"]


class TestInvalidUsername:
    @pytest.mark.parametrize(
        "username", ["ab", "a" * 21, "has space", "has-hyphen", "_leading", "admin"]
    )
    def test_returns_422(self, client: TestClient, username: str) -> None:
        response = client.post("/api/v1/auth/register", json=VALID_BODY | {"username": username})
        assert response.status_code == 422

    def test_carries_the_invalid_username_code(self, client: TestClient) -> None:
        response = client.post("/api/v1/auth/register", json=VALID_BODY | {"username": "ab"})
        assert response.json()["code"] == "invalid_username"


class TestInvalidEmail:
    @pytest.mark.parametrize("email", ["not-an-email", "no@domain", "@example.com"])
    def test_returns_422(self, client: TestClient, email: str) -> None:
        response = client.post("/api/v1/auth/register", json=VALID_BODY | {"email": email})
        assert response.status_code == 422

    def test_carries_the_invalid_email_code(self, client: TestClient) -> None:
        response = client.post("/api/v1/auth/register", json=VALID_BODY | {"email": "nope"})
        assert response.json()["code"] == "invalid_email"


class TestRequestShape:
    def test_a_client_cannot_supply_its_own_password_hash(self, client: TestClient) -> None:
        """`extra="forbid"` matters here specifically: a caller choosing
        its own pre-computed hash would bypass the password policy and the
        platform's hashing parameters entirely."""
        response = client.post(
            "/api/v1/auth/register",
            json=VALID_BODY | {"password_hash": "$argon2id$whatever"},
        )

        assert response.status_code == 422
        assert "password_hash" in response.json()["message"]

    def test_an_unknown_field_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/v1/auth/register", json=VALID_BODY | {"is_admin": True})

        assert response.status_code == 422
        assert "is_admin" in response.json()["message"]

    def test_cannot_self_grant_verification(self, client: TestClient) -> None:
        response = client.post("/api/v1/auth/register", json=VALID_BODY | {"is_verified": True})
        assert response.status_code == 422
