"""The `users` HTTP surface.

In `tests/unit/` rather than `tests/e2e/` because it matches services.md
§1's definition of that directory exactly — "no I/O, fakes only". The
database is replaced by `FakeUserRepository` through FastAPI's
`dependency_overrides`, which is the *supported* seam for this and is why
`presentation/dependencies` exists as its own module rather than the
router constructing a service inline.

What this layer is responsible for, and therefore what is asserted here:
routing, status codes, the request/response envelope, schema validation,
and the domain-exception-to-HTTP mapping. Business behaviour is
`test_user_service.py`'s job and is not re-asserted; storage behaviour is
`tests/contract/test_user_repository.py`'s and is not re-asserted either.

A genuine end-to-end test over HTTP *and* a real database belongs in
`tests/e2e/` once `auth` gives it a journey worth the setup cost — right
now it would assert the same things these do, more slowly.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from types import TracebackType
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.app_factory import create_app
from app.core.enums import Locale
from app.database.unit_of_work import SessionUnitOfWork  # noqa: F401 — see _NullUnitOfWork
from app.modules.users.application.services import UserService
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import (
    DisplayName,
    Email,
    Timezone,
    Username,
)
from app.modules.users.presentation.dependencies import get_user_service
from tests.fakes.user_repository import FakeUserRepository

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


class _NullUnitOfWork:
    """Commits nothing, because there is nothing to commit — the fake
    repository holds its state in a dict. Substituting a real
    `SessionUnitOfWork` here would need a database connection, which is
    exactly what these tests exist to avoid."""

    async def __aenter__(self) -> "_NullUnitOfWork":
        return self

    # Signature spelled out rather than `*args`: `UnitOfWork` declares
    # three named parameters, and a `*args` stand-in does not actually
    # satisfy the protocol — Pyright rejects it, correctly, because a
    # caller passing them by keyword would fail.
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


def make_user(
    *,
    username: str = "player_one",
    email: str = "player.one@example.com",
    display_name: str | None = None,
) -> User:
    return User.create(
        username=Username(username),
        email=Email(email),
        password_hash="argon2id$fake$notarealhash",
        preferred_language=Locale.EN,
        timezone=Timezone("UTC"),
        created_at=_NOW,
        display_name=DisplayName(display_name) if display_name else None,
    )


@pytest.fixture
def repository() -> FakeUserRepository:
    return FakeUserRepository()


@pytest.fixture
def client(repository: FakeUserRepository) -> Iterator[TestClient]:
    """The real application, with only the service's *construction*
    overridden. Routing, middleware, the response envelope and every
    exception handler are the production ones — which is the point: a test
    that stubbed the route would prove nothing about them."""
    app = create_app()

    def _service_override() -> UserService:
        return UserService(
            users=repository,
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
        )

    app.dependency_overrides[get_user_service] = _service_override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestGetUser:
    async def test_returns_the_user_in_the_platform_envelope(
        self, client: TestClient, repository: FakeUserRepository
    ) -> None:
        user = await repository.create(make_user())

        response = client.get(f"/api/v1/users/{user.id}")

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["username"] == "player_one"
        # A64-008's envelope, identical to every other endpoint.
        assert body["meta"]["request_id"]
        assert body["meta"]["correlation_id"]

    async def test_never_exposes_the_password_hash(
        self, client: TestClient, repository: FakeUserRepository
    ) -> None:
        # The single most important assertion in this file.
        user = await repository.create(make_user())

        response = client.get(f"/api/v1/users/{user.id}")

        assert "password_hash" not in response.text
        assert "password" not in response.json()["data"]

    def test_404_for_an_unknown_id(self, client: TestClient) -> None:
        response = client.get(f"/api/v1/users/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_422_for_a_malformed_id(self, client: TestClient) -> None:
        response = client.get("/api/v1/users/not-a-uuid")

        assert response.status_code == 422
        body = response.json()
        # Goes through the platform envelope, not FastAPI's native
        # `{"detail": [...]}` — see `_handle_request_validation_error`.
        assert body["code"] == "validation_error"
        assert "user_id" in body["message"]


class TestListUsers:
    def test_empty(self, client: TestClient) -> None:
        response = client.get("/api/v1/users")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["page"]["has_more"] is False

    async def test_returns_summaries_without_email(
        self, client: TestClient, repository: FakeUserRepository
    ) -> None:
        await repository.create(make_user())

        response = client.get("/api/v1/users")

        item = response.json()["data"]["items"][0]
        assert item["username"] == "player_one"
        # A listing has no business handing out addresses per row.
        assert "email" not in item
        assert "password_hash" not in item

    async def test_pages_with_an_opaque_cursor(
        self, client: TestClient, repository: FakeUserRepository
    ) -> None:
        for index in range(3):
            await repository.create(
                make_user(username=f"player_{index}", email=f"p{index}@example.com")
            )

        first = client.get("/api/v1/users?limit=2").json()["data"]
        assert len(first["items"]) == 2
        assert first["page"]["has_more"] is True

        second = client.get(f"/api/v1/users?limit=2&cursor={first['page']['next_cursor']}").json()[
            "data"
        ]
        assert len(second["items"]) == 1
        assert second["page"]["has_more"] is False

    async def test_filters_by_is_active(
        self, client: TestClient, repository: FakeUserRepository
    ) -> None:
        await repository.create(make_user(username="active_one", email="a@example.com"))
        inactive = make_user(username="inactive_one", email="b@example.com")
        inactive.deactivate()
        await repository.create(inactive)

        response = client.get("/api/v1/users?is_active=false")

        items = response.json()["data"]["items"]
        assert [item["username"] for item in items] == ["inactive_one"]

    def test_422_for_a_limit_beyond_the_maximum(self, client: TestClient) -> None:
        response = client.get("/api/v1/users?limit=101")

        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"


# --- removed in A64-012.3 ----------------------------------------------------
#
# This file previously covered `PATCH /users/{user_id}`, which A64-012.3
# retired: it was unauthenticated, keyed on a public id, and would have made
# "only the profile owner may edit" false the moment `PATCH /profile`
# shipped beside it (see `users/presentation/router.py`).
#
# Every behaviour those tests asserted now lives in
# `tests/contract/test_profile_editing_api.py` against the replacement
# endpoint, including the two worth keeping by name: an unknown field is
# rejected rather than ignored, and `username` cannot be changed through a
# profile update.


class TestOpenApi:
    def test_users_routes_are_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/users" in paths
        assert "/api/v1/users/{user_id}" in paths

    def test_no_schema_exposes_a_password_hash_on_a_response(self, client: TestClient) -> None:
        """Guards the leak at the contract level, not just per response:
        a future field added to `UserRead` would show up here."""
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

        # `UserSummary` is no longer rendered onto any response —
        # A64-012.6 replaced it with `PublicUserResponse` on both `users`
        # routes, so it is a published *DTO* again rather than a wire
        # shape and does not appear in the OpenAPI components.
        for name in ("UserRead", "PublicUserResponse"):
            assert "password_hash" not in schemas[name]["properties"]
