"""`GET /profiles/{username}` end to end — real PostgreSQL, the real
composition root.

`tests/unit/test_profile_service.py` covers the composition with an
in-memory repository. This file covers the thing it cannot: that the
username actually folds *in the database*, that the response serialises to
the documented shape, and that nothing from the `users` table other than
the published fields reaches the wire.

The case-insensitivity assertion is the one that most needs a real
database. Folding is a `Computed` column PostgreSQL populates
(`username_folded`), so a fake repository proves only that Python's
`casefold` agrees with itself — the question is whether the *query* matches
what the *generated column* holds.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.app_factory import create_app
from app.core.enums import Locale
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import (
    Bio,
    CountryCode,
    DisplayName,
    Email,
    Timezone,
    Username,
)
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository

JOINED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
BIO = "I play chess.\nSometimes well."


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app with only the session redirected into the test's
    rolled-back transaction. No `dependency_overrides` on any service — the
    graph under test is the one that ships."""
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield contract_session

    app.dependency_overrides[get_db_session] = _session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def player(contract_session: AsyncSession) -> User:
    """One account with every presentational field populated.

    Written straight through the repository because no endpoint sets `bio`
    or `country` yet — profile editing is a later task. That is the whole
    reason this fixture exists rather than registering through the API:
    without it, the two fields would only ever be tested as `null`.

    A mixed-case username, deliberately, so the case-folding assertions
    have something to fold.
    """
    suffix = uuid4().hex[:8]
    user = User.create(
        username=Username(f"Player{suffix}"),
        email=Email(f"{suffix}@example.com"),
        password_hash="argon2id$fake$notarealhash",
        preferred_language=Locale.EN,
        timezone=Timezone("Europe/London"),
        created_at=JOINED_AT,
    )
    user.display_name = DisplayName("Player One")
    user.bio = Bio(BIO)
    user.country = CountryCode("GB")

    created = await SqlAlchemyUserRepository(contract_session).create(user)
    await contract_session.flush()
    return created


def profile_url(username: str) -> str:
    return f"/api/v1/profiles/{username}"


class TestSuccessfulLookup:
    async def test_returns_200(self, client: AsyncClient, player: User) -> None:
        response = await client.get(profile_url(player.username.value))

        assert response.status_code == 200, response.text

    async def test_returns_every_documented_field(self, client: AsyncClient, player: User) -> None:
        """The field list A64-012.1 specifies, asserted as a set so that a
        field quietly disappearing fails here rather than in a client."""
        data: dict[str, Any] = (await client.get(profile_url(player.username.value))).json()["data"]

        assert set(data) == {
            "id",
            "username",
            "display_name",
            "avatar_url",
            "thumbnail_url",
            "country",
            "language",
            "bio",
            "joined_at",
            "last_seen",
            "ratings",
            "statistics",
        }

    async def test_returns_the_stored_values(self, client: AsyncClient, player: User) -> None:
        data = (await client.get(profile_url(player.username.value))).json()["data"]

        assert data["id"] == str(player.id)
        assert data["username"] == player.username.value
        assert data["display_name"] == "Player One"
        assert data["country"] == "GB"
        assert data["language"] == "en"
        assert data["bio"] == BIO
        assert data["joined_at"].startswith("2026-08-01T12:00:00")

    async def test_ratings_and_statistics_have_the_documented_shape(
        self, client: AsyncClient, player: User
    ) -> None:
        """Placeholder *values*, final *shape* — a client written against
        this needs no change when the rating system ships."""
        data = (await client.get(profile_url(player.username.value))).json()["data"]

        assert set(data["ratings"]) == {"classic", "rapid", "blitz"}
        for category in data["ratings"].values():
            assert category == {"rating": 1500, "is_provisional": True, "games_played": 0}

        assert data["statistics"] == {
            "games_played": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "win_rate": 0.0,
        }

    async def test_last_seen_is_null(self, client: AsyncClient, player: User) -> None:
        data = (await client.get(profile_url(player.username.value))).json()["data"]

        assert data["last_seen"] is None

    async def test_needs_no_authentication(self, client: AsyncClient, player: User) -> None:
        """No `Authorization` header is sent anywhere in this file, so every
        assertion above is already an anonymous request. Stated explicitly
        because "public" is the property most easily lost by a later edit
        adding a router-level guard."""
        response = await client.get(profile_url(player.username.value))

        assert response.status_code == 200
        assert "www-authenticate" not in response.headers


class TestNothingPrivateEscapes:
    async def test_the_response_carries_no_email(self, client: AsyncClient, player: User) -> None:
        """Asserted against the raw response text rather than the parsed
        `data`, because an address leaking through `meta`, an error field or
        a header would be just as much of a disclosure."""
        response = await client.get(profile_url(player.username.value))

        assert player.email.value not in response.text
        assert "email" not in response.text

    async def test_the_response_carries_no_credential_or_account_state(
        self, client: AsyncClient, player: User
    ) -> None:
        """`password_hash`, `is_verified`, `locked_until` and `timezone` are
        all on the row this profile was built from. None is published: the
        first is a credential, the next two are account state that tells an
        attacker which accounts are half-registered or under attack, and the
        last narrows a player's physical location."""
        response = await client.get(profile_url(player.username.value))

        for forbidden in (
            "password",
            "argon2id",
            "is_verified",
            "is_active",
            "locked_until",
            "timezone",
            "updated_at",
        ):
            assert forbidden not in response.text, f"{forbidden!r} leaked into the response"


class TestProfileNotFound:
    async def test_an_unknown_username_returns_404(self, client: AsyncClient) -> None:
        response = await client.get(profile_url(f"nobody{uuid4().hex[:8]}"))

        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    async def test_a_deactivated_account_is_indistinguishable_from_missing(
        self, client: AsyncClient, player: User, contract_session: AsyncSession
    ) -> None:
        """Publishing which accounts are withdrawn tells an impersonator
        whose handle is safe to adopt. Compared to a genuine miss rather
        than to a constant, because the failure guarded against is a
        *difference*."""
        player.deactivate()
        await SqlAlchemyUserRepository(contract_session).update(player)
        await contract_session.flush()

        deactivated = await client.get(profile_url(player.username.value))
        missing = await client.get(profile_url(f"nobody{uuid4().hex[:8]}"))

        assert deactivated.status_code == missing.status_code == 404
        assert deactivated.json()["code"] == missing.json()["code"]
        assert deactivated.json()["message"] == missing.json()["message"]

    async def test_a_malformed_username_is_422(self, client: AsyncClient) -> None:
        """Rejected by the path parameter's own bounds before any lookup
        happens — the right feedback for a typo, and it reveals nothing: a
        name that cannot be a handle cannot belong to anyone."""
        assert (await client.get(profile_url("a"))).status_code == 422


class TestCaseInsensitiveLookup:
    async def test_every_casing_resolves_to_the_same_profile(
        self, client: AsyncClient, player: User
    ) -> None:
        """UP-1, against the real generated `username_folded` column.

        A fake repository can only prove that Python's `casefold` agrees
        with itself; the question here is whether the query matches what
        PostgreSQL computed on insert.
        """
        registered = player.username.value

        for queried in (registered, registered.lower(), registered.upper()):
            response = await client.get(profile_url(queried))

            assert response.status_code == 200, f"{queried!r} -> {response.status_code}"
            assert response.json()["data"]["id"] == str(player.id)

    async def test_the_response_preserves_the_registered_casing(
        self, client: AsyncClient, player: User
    ) -> None:
        """Matching folds; rendering does not. A visitor who types the
        handle in lower case still sees the name the player chose."""
        response = await client.get(profile_url(player.username.value.upper()))

        assert response.json()["data"]["username"] == player.username.value


class TestOpenApi:
    async def test_the_endpoint_is_documented(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()
        operation = spec["paths"]["/api/v1/profiles/{username}"]["get"]

        assert operation["summary"]
        assert operation["description"].strip()
        assert operation["tags"] == ["profiles"]
        assert set(operation["responses"]) >= {"200", "404", "422"}

    async def test_the_error_responses_carry_the_platform_error_model(
        self, client: AsyncClient
    ) -> None:
        spec = (await client.get("/openapi.json")).json()
        operation = spec["paths"]["/api/v1/profiles/{username}"]["get"]

        for status in ("404", "422"):
            schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert "ErrorResponse" in str(schema)

    async def test_the_response_schema_carries_an_example(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()

        assert spec["components"]["schemas"]["ProfileResponse"].get("examples")
