"""Statistics end to end — real PostgreSQL, the real composition root.

A64-012.6 asks for essential tests only and names four: statistics
returned, the fallback provider, hidden statistics, and a profile owner
always seeing their own. All four are here.

What else is here is the small set of properties that would be *silently*
wrong rather than loudly broken:

  - **the numbers come from the table, not from a default** — asserted by
    writing a row with nine distinct values and reading them back through
    the whole stack, which is the only way to tell a working provider from
    one that always returns zeroes;
  - **`win_rate` is derived** — a stored copy could disagree with the
    counts printed beside it, so the response's value is checked against
    the counts in the same response;
  - **absence is a value** — a player with no row reports the empty record
    rather than a 404 or a `null`;
  - **the fallback is indistinguishable from a new player**, which is the
    honest cost of the kill switch and is worth pinning so nobody assumes
    otherwise;
  - **hiding is a `null` object, never zeroes** — a zeroed record reads as
    a beginner and would misinform an opponent.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_rate_limiter, get_statistics_settings
from app.app_factory import create_app
from app.config.settings import StatisticsSettings
from app.modules.statistics.infrastructure.models import PlayerStatisticsModel
from tests.fakes.rate_limiter import AllowAllRateLimiter

PROFILES_URL = "/api/v1/profiles"
ME_URL = "/api/v1/profile/me"
PRIVACY_URL = "/api/v1/profile/privacy"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"

#: What a player with no stored row reports. Typed in by hand rather than
#: imported from the domain, so this asserts the contract rather than that
#: the code agrees with itself.
EMPTY_RECORD = {
    "games_played": 0,
    "wins": 0,
    "losses": 0,
    "draws": 0,
    "win_rate": 0.0,
    "current_rating": 1500,
    "highest_rating": 1500,
    "current_streak": 0,
    "best_win_streak": 0,
}

#: A record with nine *different* numbers, so a provider that returned
#: defaults could not accidentally pass. 6 wins of 10 is a win rate of 0.6
#: — draws in the denominator, per the documented definition.
A_REAL_RECORD = {
    "games_played": 10,
    "wins": 6,
    "losses": 3,
    "draws": 1,
    "current_rating": 1620,
    "highest_rating": 1655,
    "current_streak": 2,
    "best_win_streak": 4,
}
A_REAL_RECORD_RESPONSE = {**A_REAL_RECORD, "win_rate": 0.6}


def _app(session: AsyncSession, *, statistics_enabled: bool = True) -> Any:
    """The production app with the session, the rate limiter and — only
    where a test needs it — the statistics kill switch redirected.

    No `dependency_overrides` on any provider or service: the graph under
    test is the one that ships, including the real
    `DatabaseStatisticsProvider` reading the real table.
    """
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_rate_limiter] = lambda: AllowAllRateLimiter()
    if not statistics_enabled:
        app.dependency_overrides[get_statistics_settings] = lambda: StatisticsSettings(
            enabled=False
        )
    return app


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = _app(contract_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def fallback_client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The same app with `STATISTICS_ENABLED=false`, which wires
    `NoMatchesStatisticsProvider` instead of the database one."""
    app = _app(contract_session, statistics_enabled=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


async def register(client: AsyncClient) -> tuple[str, UUID, dict[str, str]]:
    """One account. Returns its username, its player id and an
    `Authorization` header."""
    suffix = uuid4().hex[:10]
    username = f"player{suffix}"
    account = {
        "username": username,
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }
    registered = await client.post(REGISTER_URL, json=account)
    assert registered.status_code == 201, registered.text
    player_id = UUID(registered.json()["data"]["id"])

    signed_in = await client.post(LOGIN_URL, json={"email": account["email"], "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    auth = {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"}
    return username, player_id, auth


@pytest_asyncio.fixture
async def account(client: AsyncClient) -> tuple[str, UUID, dict[str, str]]:
    return await register(client)


async def store_record(session: AsyncSession, player_id: UUID, **record: int) -> None:
    """Writes a row straight into `statistics.player_statistics`.

    Through the table rather than through an endpoint, because there is no
    endpoint: A64-012.6 builds the *reading* half and explicitly excludes
    game result processing. This stands in for the `match.completed`
    consumer that will eventually write these rows.
    """
    await session.execute(insert(PlayerStatisticsModel).values(player_id=player_id, **record))
    await session.flush()


class TestStatisticsReturned:
    """A64-012.6's first required test."""

    async def test_a_stored_record_reaches_the_public_profile(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        account: tuple[str, UUID, dict[str, str]],
    ) -> None:
        """Nine distinct numbers, so a provider still returning defaults
        could not pass by coincidence."""
        username, player_id, _ = account
        await store_record(contract_session, player_id, **A_REAL_RECORD)

        response = await client.get(f"{PROFILES_URL}/{username}")

        assert response.status_code == 200, response.text
        assert response.json()["data"]["statistics"] == A_REAL_RECORD_RESPONSE

    async def test_win_rate_is_derived_from_the_counts_in_the_same_response(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        account: tuple[str, UUID, dict[str, str]],
    ) -> None:
        """A stored `win_rate` would be a number that can disagree with the
        counts beside it. This is the assertion that would catch it."""
        username, player_id, _ = account
        await store_record(contract_session, player_id, **A_REAL_RECORD)

        statistics = (await client.get(f"{PROFILES_URL}/{username}")).json()["data"]["statistics"]

        assert statistics["win_rate"] == round(statistics["wins"] / statistics["games_played"], 4)

    async def test_a_player_with_no_row_reports_the_empty_record(
        self, client: AsyncClient, account: tuple[str, UUID, dict[str, str]]
    ) -> None:
        """Absence is a value, not a failure. A projection is built by
        folding results in, so an account that has played nothing has no
        row — which is every account on the day it registers."""
        username, _, _ = account

        response = await client.get(f"{PROFILES_URL}/{username}")

        assert response.status_code == 200, response.text
        assert response.json()["data"]["statistics"] == EMPTY_RECORD


class TestFallbackProvider:
    """A64-012.6's second required test."""

    async def test_the_fallback_reports_the_empty_record_over_a_real_one(
        self,
        fallback_client: AsyncClient,
        client: AsyncClient,
        contract_session: AsyncSession,
    ) -> None:
        """With `STATISTICS_ENABLED=false` the stored row is not read at
        all — proved by storing one and getting zeroes back.

        This is also the honest statement of what the switch costs: a
        player with a real record is indistinguishable from a brand-new
        account while it is off. The composition root logs at `WARNING` on
        every request so an operator can see it; a client cannot.
        """
        username, player_id, _ = await register(client)
        await store_record(contract_session, player_id, **A_REAL_RECORD)

        served = await client.get(f"{PROFILES_URL}/{username}")
        degraded = await fallback_client.get(f"{PROFILES_URL}/{username}")

        assert served.json()["data"]["statistics"] == A_REAL_RECORD_RESPONSE
        assert degraded.json()["data"]["statistics"] == EMPTY_RECORD

    async def test_the_fallback_still_serves_the_rest_of_the_profile(
        self, fallback_client: AsyncClient, client: AsyncClient
    ) -> None:
        """The whole point of degrading rather than failing: the platform's
        highest-volume public read keeps working when a rebuildable
        projection does not."""
        username, _, _ = await register(client)

        response = await fallback_client.get(f"{PROFILES_URL}/{username}")

        assert response.status_code == 200, response.text
        assert response.json()["data"]["username"] == username
        assert response.json()["data"]["ratings"]["blitz"]["rating"] == 1500


class TestHiddenStatistics:
    """A64-012.6's third required test — reusing A64-012.4's gate, not a
    second one."""

    async def test_a_hidden_record_is_null_on_the_public_profile(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        account: tuple[str, UUID, dict[str, str]],
    ) -> None:
        username, player_id, auth = account
        await store_record(contract_session, player_id, **A_REAL_RECORD)

        await client.patch(PRIVACY_URL, headers=auth, json={"show_statistics": False})

        response = await client.get(f"{PROFILES_URL}/{username}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["statistics"] is None

    async def test_a_hidden_record_is_never_zeroed(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        account: tuple[str, UUID, dict[str, str]],
    ) -> None:
        """`null`, not a zeroed record. Zeroes are what a genuine beginner
        has, so publishing them for a player who opted out would misinform
        the opponent deciding whether to accept a challenge — worse than
        publishing nothing."""
        username, player_id, auth = account
        await store_record(contract_session, player_id, **A_REAL_RECORD)
        await client.patch(PRIVACY_URL, headers=auth, json={"show_statistics": False})

        statistics = (await client.get(f"{PROFILES_URL}/{username}")).json()["data"]["statistics"]

        assert statistics is None
        assert statistics != EMPTY_RECORD

    async def test_ratings_survive_a_hidden_record(
        self, client: AsyncClient, account: tuple[str, UUID, dict[str, str]]
    ) -> None:
        """UP-5: privacy governs discovery, not the rated results
        themselves. `show_statistics` covers the record of games, never the
        rating computed from them."""
        username, _, auth = account
        await client.patch(PRIVACY_URL, headers=auth, json={"show_statistics": False})

        profile = (await client.get(f"{PROFILES_URL}/{username}")).json()["data"]

        assert profile["statistics"] is None
        assert profile["ratings"]["blitz"]["rating"] == 1500


class TestOwnerAlwaysSeesTheirOwn:
    """A64-012.6's fourth required test."""

    async def test_the_owner_sees_a_record_they_have_hidden(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        account: tuple[str, UUID, dict[str, str]],
    ) -> None:
        """`show_statistics` governs what a *stranger* sees. A control that
        hid a record from the person who hid it would be one nobody could
        verify they had set."""
        username, player_id, auth = account
        await store_record(contract_session, player_id, **A_REAL_RECORD)
        await client.patch(PRIVACY_URL, headers=auth, json={"show_statistics": False})

        mine = (await client.get(ME_URL, headers=auth)).json()["data"]
        public = (await client.get(f"{PROFILES_URL}/{username}")).json()["data"]

        assert mine["statistics"] == A_REAL_RECORD_RESPONSE
        assert public["statistics"] is None

    async def test_the_owner_view_never_reports_null(
        self, client: AsyncClient, account: tuple[str, UUID, dict[str, str]]
    ) -> None:
        """Required rather than nullable on `MyProfileResponse`: the public
        shape has to express "hidden" and this one never does, so a client
        rendering a settings screen needs no null check."""
        _, _, auth = account
        await client.patch(PRIVACY_URL, headers=auth, json={"show_statistics": False})

        response = await client.get(ME_URL, headers=auth)

        assert response.status_code == 200, response.text
        assert response.json()["data"]["statistics"] == EMPTY_RECORD

    async def test_a_profile_edit_returns_the_record_too(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        account: tuple[str, UUID, dict[str, str]],
    ) -> None:
        """`PATCH /profile` shares `MyProfileResponse`, so the response is
        one coherent view of the account rather than an edited profile
        beside a record fetched separately."""
        _, player_id, auth = account
        await store_record(contract_session, player_id, **A_REAL_RECORD)

        response = await client.patch("/api/v1/profile", headers=auth, json={"bio": "Hello."})

        assert response.status_code == 200, response.text
        assert response.json()["data"]["statistics"] == A_REAL_RECORD_RESPONSE


class TestPrivacyIsNotDuplicated:
    async def test_the_public_path_does_not_read_a_hidden_record(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        account: tuple[str, UUID, dict[str, str]],
    ) -> None:
        """A hidden record is not fetched and discarded — it is never
        fetched. Asserted through the one observable consequence: a stored
        row that would otherwise appear does not, and the owner's own view
        still reads the same row, so the gate is on the composition rather
        than on the store."""
        username, player_id, auth = account
        await store_record(contract_session, player_id, **A_REAL_RECORD)
        await client.patch(PRIVACY_URL, headers=auth, json={"show_statistics": False})

        public = (await client.get(f"{PROFILES_URL}/{username}")).json()["data"]
        owner = (await client.get(ME_URL, headers=auth)).json()["data"]

        assert public["statistics"] is None
        assert owner["statistics"]["games_played"] == 10


class TestOpenApi:
    def test_every_statistics_field_is_documented(self) -> None:
        schema = create_app().openapi()
        properties = schema["components"]["schemas"]["StatisticsResponse"]["properties"]

        assert set(properties) == set(EMPTY_RECORD)
        for name, spec in properties.items():
            assert spec.get("description"), name
            assert spec.get("examples"), name

    def test_the_public_profile_documents_a_nullable_record(self) -> None:
        schema = create_app().openapi()
        statistics = schema["components"]["schemas"]["ProfileResponse"]["properties"]["statistics"]

        assert {"type": "null"} in statistics["anyOf"]
        assert "null" in statistics["description"]

    def test_the_owner_view_documents_a_required_record(self) -> None:
        """The asymmetry is the contract: hidden is expressible on one
        shape and not on the other."""
        schema = create_app().openapi()["components"]["schemas"]["MyProfileResponse"]

        assert "statistics" in schema["required"]
        assert "anyOf" not in schema["properties"]["statistics"]
