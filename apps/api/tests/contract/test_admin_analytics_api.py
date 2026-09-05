"""The admin analytics API — A64-027.6.

Three things this endpoint family must get right, and each fails in a way
nobody notices from the console:

    authorization      a page guard protects a page. It does not protect an
                       endpoint anybody can call with a normal token
    the bound          an administrator holds credentials and can call
                       these repeatedly. An unbounded range is a denial of
                       service with a valid token
    the payload        an analytics response that could name a person is a
                       surveillance tool with a chart on it

Over real PostgreSQL and the real router, because an authorization boundary
asserted against a mock is a boundary asserted against the mock.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.environment import Environment
from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.infrastructure.models import RoleAssignmentModel
from app.modules.analytics.domain.event import AnalyticsEvent
from app.modules.analytics.domain.subject import SubjectKey
from app.modules.analytics.infrastructure.repositories.analytics_repository import (
    SqlAlchemyAnalyticsEventStore,
)
from app.platform.analytics import EventName
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register as register_account

BASE = "/api/v1/admin/analytics"
SECTIONS = ("overview", "acquisition", "retention", "matchmaking", "games")

#: Every identifier an aggregate response must never contain. Names rather
#: than a shape check, because a leak arrives as a field somebody added in
#: good faith — "just the match id, for debugging".
FORBIDDEN_KEYS = frozenset(
    {
        "email",
        "username",
        "display_name",
        "player_id",
        "user_id",
        "subject_key",
        "subject_id",
        "anonymous_id",
        "session_id",
        "match_id",
        "queue_ticket_id",
        "ticket_id",
        "offer_id",
        "ip",
        "ip_address",
        "user_agent",
    }
)


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def make_admin(session: AsyncSession, client: AsyncClient):  # type: ignore[no-untyped-def]
    account = await register_account(client, session)
    session.add(
        RoleAssignmentModel(
            id=uuid4(),
            account_id=account.id,
            role=AdminRole.ADMIN,
            granted_by=None,
            granted_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return account


def _keys(payload: object) -> set[str]:
    """Every key anywhere in the response, however deeply nested."""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _keys(item)
    return found


class TestAuthorization:
    """**MUTATION A targets these.**"""

    @pytest.mark.parametrize("section", SECTIONS)
    async def test_an_anonymous_caller_is_refused(self, client: AsyncClient, section: str) -> None:
        assert (await client.get(f"{BASE}/{section}")).status_code == 401

    @pytest.mark.parametrize("section", SECTIONS)
    async def test_a_normal_account_is_refused(
        self, client: AsyncClient, contract_session: AsyncSession, section: str
    ) -> None:
        """A page guard in `apps/admin` protects a page. This is the
        boundary that protects the data."""
        viewer = await register_account(client, contract_session)
        response = await client.get(f"{BASE}/{section}", headers=viewer.auth)
        assert response.status_code == 403, response.text

    @pytest.mark.parametrize("section", SECTIONS)
    async def test_an_administrator_is_allowed(
        self, client: AsyncClient, contract_session: AsyncSession, section: str
    ) -> None:
        admin = await make_admin(contract_session, client)
        response = await client.get(f"{BASE}/{section}", headers=admin.auth)
        assert response.status_code == 200, response.text

    async def test_there_is_no_raw_event_endpoint(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§6. Enforced by the absence of a route rather than by a filter
        somebody could relax."""
        admin = await make_admin(contract_session, client)
        for path in ("/events", "/raw", "/subjects"):
            response = await client.get(f"{BASE}{path}", headers=admin.auth)
            assert response.status_code == 404, f"{path}: {response.text}"


class TestTheBoundedRange:
    """**MUTATION I targets these.**"""

    async def test_a_backwards_range_is_refused(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        response = await client.get(
            f"{BASE}/games",
            params={"start": "2026-03-01", "end": "2026-02-01"},
            headers=admin.auth,
        )
        assert response.status_code == 422, response.text

    async def test_an_unbounded_range_is_refused(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """`from=1970&to=9999` would scan the whole store on an endpoint an
        administrator can call in a loop."""
        admin = await make_admin(contract_session, client)
        response = await client.get(
            f"{BASE}/games",
            params={"start": "1970-01-01", "end": "9999-12-31"},
            headers=admin.auth,
        )
        assert response.status_code == 422, response.text

    async def test_the_cap_is_ninety_days(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        from app.modules.admin.presentation.routers.analytics import MAX_RANGE_DAYS

        admin = await make_admin(contract_session, client)
        end = date(2026, 4, 1)
        inside = await client.get(
            f"{BASE}/games",
            params={"start": str(end - timedelta(days=MAX_RANGE_DAYS - 1)), "end": str(end)},
            headers=admin.auth,
        )
        outside = await client.get(
            f"{BASE}/games",
            params={"start": str(end - timedelta(days=MAX_RANGE_DAYS)), "end": str(end)},
            headers=admin.auth,
        )
        assert inside.status_code == 200, inside.text
        assert outside.status_code == 422, outside.text

    async def test_the_default_range_ends_yesterday(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§9. Including today would put a partial day beside thirty whole
        ones and make every morning look like a collapse."""
        admin = await make_admin(contract_session, client)
        payload = (await client.get(f"{BASE}/games", headers=admin.auth)).json()

        end = date.fromisoformat(payload["meta"]["requested_end"])
        start = date.fromisoformat(payload["meta"]["requested_start"])
        assert end < datetime.now(UTC).date()
        assert (end - start).days == 29


class TestThePayloadNamesNobody:
    """**MUTATION E targets these.**"""

    @pytest.mark.parametrize("section", SECTIONS)
    async def test_no_response_carries_an_identifier(
        self, client: AsyncClient, contract_session: AsyncSession, section: str
    ) -> None:
        admin = await make_admin(contract_session, client)
        payload = (await client.get(f"{BASE}/{section}", headers=admin.auth)).json()

        leaked = _keys(payload) & FORBIDDEN_KEYS
        assert leaked == set(), f"{section} leaked {sorted(leaked)}"

    async def test_a_populated_response_still_names_nobody(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The empty case proves the schema; this proves the mapper — a
        response with rows in it is where a `match_id` would appear."""
        admin = await make_admin(contract_session, client)
        day = datetime.now(UTC).date() - timedelta(days=3)
        subject = SubjectKey(uuid4())
        match_id = str(uuid4())
        at = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=10)

        await SqlAlchemyAnalyticsEventStore(contract_session).append(
            [
                AnalyticsEvent(
                    event_id=uuid4(),
                    event_name=EventName.MATCH_STARTED,
                    event_version=1,
                    occurred_at=at,
                    received_at=at,
                    source="backend",
                    environment=Environment.PRODUCTION,
                    subject_key=subject,
                    properties={"match_id": match_id, "variant": "russian_8x8", "rated": True},
                ),
                AnalyticsEvent(
                    event_id=uuid4(),
                    event_name=EventName.MATCH_COMPLETED,
                    event_version=1,
                    occurred_at=at + timedelta(minutes=20),
                    received_at=at + timedelta(minutes=20),
                    source="backend",
                    environment=Environment.PRODUCTION,
                    properties={
                        "match_id": match_id,
                        "variant": "russian_8x8",
                        "rated": True,
                        "outcome": "win",
                        "termination_reason": "resignation",
                        "ply_count": 30,
                        "origin": "queue",
                    },
                ),
            ]
        )

        response = await client.get(f"{BASE}/games", headers=admin.auth)
        payload = response.json()

        assert payload["started"] == 1
        assert payload["completed"] == 1
        assert _keys(payload) & FORBIDDEN_KEYS == set()
        # And no identifier smuggled in as a value, either.
        assert match_id not in response.text
        assert str(subject) not in response.text


class TestTheFiltersAreNotOptional:
    """**MUTATION G and H target these.**"""

    async def _store(self, session: AsyncSession, **kwargs: object) -> date:
        day = datetime.now(UTC).date() - timedelta(days=3)
        at = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=10)
        await SqlAlchemyAnalyticsEventStore(session).append(
            [
                AnalyticsEvent(
                    event_id=uuid4(),
                    event_name=EventName.QUEUE_JOINED,
                    event_version=1,
                    occurred_at=at,
                    received_at=at,
                    source="backend",
                    subject_key=SubjectKey(uuid4()),
                    properties={
                        "variant": "russian_8x8",
                        "queue_type": "ranked",
                        "rated": True,
                    },
                    **kwargs,  # type: ignore[arg-type]
                )
            ]
        )
        return day

    async def test_synthetic_traffic_is_excluded(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        await self._store(contract_session, environment=Environment.PRODUCTION)
        await self._store(contract_session, environment=Environment.PRODUCTION, is_synthetic=True)

        payload = (await client.get(f"{BASE}/matchmaking", headers=admin.auth)).json()

        assert payload["queue_joins"] == 1
        assert payload["meta"]["include_synthetic"] is False

    async def test_another_environment_is_excluded(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§11. No environment parameter exists, so a console cannot be
        pointed at staging by a query string."""
        admin = await make_admin(contract_session, client)
        await self._store(contract_session, environment=Environment.PRODUCTION)
        await self._store(contract_session, environment=Environment.STAGING)

        payload = (await client.get(f"{BASE}/matchmaking", headers=admin.auth)).json()

        assert payload["queue_joins"] == 1
        assert payload["meta"]["environment"] == "production"

    async def test_an_environment_parameter_is_ignored(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        admin = await make_admin(contract_session, client)
        await self._store(contract_session, environment=Environment.STAGING)

        payload = (
            await client.get(
                f"{BASE}/matchmaking",
                params={"environment": "staging", "include_synthetic": "true"},
                headers=admin.auth,
            )
        ).json()

        assert payload["queue_joins"] == 0
        assert payload["meta"]["environment"] == "production"


class TestTheMetadataTravels:
    async def test_every_section_carries_its_period(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """A figure without its window gets compared against one that meant
        something else."""
        admin = await make_admin(contract_session, client)
        for section in SECTIONS:
            payload = (await client.get(f"{BASE}/{section}", headers=admin.auth)).json()
            meta = payload["meta"]
            assert meta["maturity"] in {"mature", "partial"}
            assert meta["coverage"] in {"complete", "truncated"}
            assert meta["generated_at"]

    async def test_the_grain_is_on_the_wire(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§29. A console cannot label a queue-attempt rate as a share of
        players if the response says what a unit is."""
        admin = await make_admin(contract_session, client)

        queue = (await client.get(f"{BASE}/matchmaking", headers=admin.auth)).json()
        games = (await client.get(f"{BASE}/games", headers=admin.auth)).json()

        assert queue["grain"] == "queue_attempt"
        assert games["grain"] == "match"

    async def test_no_speed_class_completion_rate_exists(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """**MUTATION F.** `match_started` carries no speed class, so a
        segmented denominator does not exist — A64-027.5 §89. The field is
        absent rather than null, because a nullable one invites a console
        to render a dash where a number will never come."""
        admin = await make_admin(contract_session, client)
        payload = (await client.get(f"{BASE}/games", headers=admin.auth)).json()

        assert "completion_rate_by_speed_class" not in payload
        assert not any("speed" in key for key in _keys(payload))

    async def test_an_empty_store_returns_nulls_and_not_zeros(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """**MUTATION B's backend half.** An undefined rate must reach the
        console as `null`, or the console cannot tell "nobody converted"
        from "there was nothing to convert"."""
        admin = await make_admin(contract_session, client)
        payload = (await client.get(f"{BASE}/games", headers=admin.auth)).json()

        assert payload["started"] == 0
        assert payload["completion_rate"] is None
        assert payload["resignation_rate"] is None
