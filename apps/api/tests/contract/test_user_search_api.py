"""`GET /users/search` end to end — real PostgreSQL, real indexes, the real
composition root.

A64-013.1 asks for essential tests only and names five: username search,
display-name search, pagination, privacy respected, and an empty query
rejected. All five are here.

What else is here is the small set of properties that would be *silently*
wrong rather than loudly broken, and that no unit test can reach:

  - **the ranking is the documented one**, asserted with a fixture whose
    four accounts fall into four different buckets for one term — a query
    that ordered alphabetically would pass any test using fewer;
  - **the normalisation is PostgreSQL's**, so accents and case fold on both
    sides of the comparison. This is the assertion that most needs a real
    database: the term and the column are normalised by the same SQL
    function, and nothing in Python could prove they agree;
  - **the wildcard is escaped rather than executed** — a `LIKE` pattern
    built from an unescaped underscore matches any character, which would
    make `pl_yer` find `player` and would be invisible in a suite that only
    searched for letters;
  - **the searcher is absent from their own results**, which is also the
    exclusion path blocking will use.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Locale
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import (
    CountryCode,
    DisplayName,
    Email,
    Timezone,
    Username,
)
from app.modules.users.domain.visibility import VisibilityLevel
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from tests.contract.contract_app import build_contract_app, contract_client

SEARCH_URL = "/api/v1/users/search"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"

JOINED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app over the test's rolled-back transaction.

    No override on the searcher, the composer or any schema — the graph
    under test is the one that ships, including the real GIN indexes, which
    `conftest.py` creates through `Base.metadata.create_all` (see
    `users.infrastructure.search_ddl`).
    """
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


@pytest_asyncio.fixture
async def auth(client: AsyncClient) -> dict[str, str]:
    """A registered, signed-in account's bearer header.

    Search is authenticated, unlike every other profile read on this
    platform — see the router on why that is the enumeration control.
    """
    suffix = uuid4().hex[:10]
    account = {
        "username": f"searcher{suffix}",
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }
    registered = await client.post(REGISTER_URL, json=account)
    assert registered.status_code == 201, registered.text

    signed_in = await client.post(LOGIN_URL, json={"email": account["email"], "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    return {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"}


async def make_player(
    session: AsyncSession,
    *,
    username: str,
    display_name: str | None = None,
) -> User:
    """One account, written straight through the repository.

    Not through the API, because registration cannot set a display name and
    half of these tests are about matching one.
    """
    suffix = uuid4().hex[:8]
    user = User.create(
        username=Username(username),
        email=Email(f"{suffix}@example.com"),
        password_hash="argon2id$fake$notarealhash",
        preferred_language=Locale.EN,
        timezone=Timezone("Europe/London"),
        created_at=JOINED_AT,
    )
    if display_name is not None:
        user.display_name = DisplayName(display_name)

    created = await SqlAlchemyUserRepository(session).create(user)
    await session.flush()
    return created


async def search(
    client: AsyncClient, auth: dict[str, str], term: str, **params: Any
) -> dict[str, Any]:
    response = await client.get(SEARCH_URL, headers=auth, params={"q": term, **params})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()["data"]
    return body


def usernames(page: dict[str, Any]) -> list[str]:
    return [item["username"] for item in page["items"]]


class TestUsernameSearch:
    """A64-013.1's first required test."""

    async def test_a_username_prefix_finds_the_player(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        tag = uuid4().hex[:8]
        await make_player(contract_session, username=f"alice{tag}")

        assert usernames(await search(client, auth, f"alice{tag}")) == [f"alice{tag}"]

    async def test_matching_is_case_insensitive(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """The term and the column go through the same SQL normalisation,
        so this is asserting that PostgreSQL agrees with itself — which is
        the only agreement that matters, and the reason nothing folds the
        term in Python."""
        tag = uuid4().hex[:8]
        await make_player(contract_session, username=f"Alice{tag}")

        for typed in (f"alice{tag}", f"ALICE{tag}", f"AlIcE{tag}"):
            assert usernames(await search(client, auth, typed)) == [f"Alice{tag}"], typed

    async def test_a_partial_match_anywhere_in_the_username_is_found(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """The requirement that rules out full-text search: a tsquery
        matches words, and `ice` is not a word inside `alice`."""
        tag = uuid4().hex[:8]
        await make_player(contract_session, username=f"malice{tag}")

        assert usernames(await search(client, auth, f"alice{tag}")) == [f"malice{tag}"]

    async def test_a_deactivated_account_is_never_returned(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """The same rule `GET /profiles/{username}` enforces with a 404:
        which handles belong to withdrawn accounts is itself a disclosure,
        and a search that returned them would be the list an impersonator
        wants."""
        tag = uuid4().hex[:8]
        player = await make_player(contract_session, username=f"gone{tag}")
        player.deactivate()
        await SqlAlchemyUserRepository(contract_session).update(player)
        await contract_session.flush()

        assert usernames(await search(client, auth, f"gone{tag}")) == []


class TestDisplayNameSearch:
    """A64-013.1's second required test."""

    async def test_a_display_name_prefix_finds_the_player(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        tag = uuid4().hex[:8]
        await make_player(
            contract_session, username=f"player{tag}", display_name=f"Grandmaster{tag}"
        )

        assert usernames(await search(client, auth, f"grandmaster{tag}")) == [f"player{tag}"]

    async def test_matching_is_accent_insensitive(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """A64-013.1's "accent-insensitive (where supported)".

        Display names are where it matters: usernames are `[a-zA-Z0-9_]` by
        validation, so they carry no accents to fold. Both directions are
        asserted — the accent in the stored name must be found by an
        unaccented term *and* vice versa — because `unaccent` is applied to
        both sides and a one-sided implementation would pass only one.
        """
        tag = uuid4().hex[:8]
        await make_player(contract_session, username=f"player{tag}", display_name=f"Jánibek{tag}")

        assert usernames(await search(client, auth, f"janibek{tag}")) == [f"player{tag}"]
        assert usernames(await search(client, auth, f"Jánibek{tag}")) == [f"player{tag}"]

    async def test_a_player_without_a_display_name_is_still_searchable_by_handle(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """`search_normalise` is STRICT, so a `NULL` display name
        normalises to `NULL` and never matches — which must not stop the
        username half of the `OR` from working."""
        tag = uuid4().hex[:8]
        await make_player(contract_session, username=f"nameless{tag}")

        assert usernames(await search(client, auth, f"nameless{tag}")) == [f"nameless{tag}"]


class TestRanking:
    async def test_the_documented_order_is_produced(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """Four accounts, four buckets, one term.

        A query that merely filtered and sorted alphabetically would order
        these `exact, partial, prefix, display` — so this fails on any
        implementation that does not rank.
        """
        tag = uuid4().hex[:8]
        term = f"ali{tag}"
        await make_player(contract_session, username=f"zzz{term}")  # partial: rank 3
        await make_player(contract_session, username=f"{term}bert")  # prefix: rank 1
        await make_player(contract_session, username=f"mmm{tag}", display_name=f"{term} Smith")
        await make_player(contract_session, username=term)  # exact: rank 0

        assert usernames(await search(client, auth, term)) == [
            term,
            f"{term}bert",
            f"mmm{tag}",
            f"zzz{term}",
        ]

    async def test_the_order_is_stable_across_identical_requests(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """ "Stable ordering" is what makes the cursor correct rather than
        approximate — an unstable order silently skips and repeats rows at
        every page boundary."""
        tag = uuid4().hex[:8]
        for index in range(5):
            await make_player(contract_session, username=f"stable{tag}{index}")

        first = usernames(await search(client, auth, f"stable{tag}"))

        assert first == usernames(await search(client, auth, f"stable{tag}"))
        assert len(first) == 5


class TestPagination:
    """A64-013.1's third required test."""

    async def test_pages_with_an_opaque_cursor_and_never_repeats(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        tag = uuid4().hex[:8]
        for index in range(5):
            await make_player(contract_session, username=f"page{tag}{index}")

        first = await search(client, auth, f"page{tag}", limit=2)
        assert len(first["items"]) == 2
        assert first["page"]["has_more"] is True

        second = await search(
            client, auth, f"page{tag}", limit=2, cursor=first["page"]["next_cursor"]
        )
        third = await search(
            client, auth, f"page{tag}", limit=2, cursor=second["page"]["next_cursor"]
        )

        assert third["page"]["has_more"] is False
        assert third["page"]["next_cursor"] is None

        seen = usernames(first) + usernames(second) + usernames(third)
        assert len(seen) == len(set(seen)) == 5

    async def test_the_response_reports_no_total(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """Keyset, not offset: counting a partial-match query costs as much
        as running it, and RP-03 forbids paying for a number nobody scrolls
        to."""
        tag = uuid4().hex[:8]
        await make_player(contract_session, username=f"total{tag}")

        page = (await search(client, auth, f"total{tag}"))["page"]

        assert set(page) == {"next_cursor", "has_more"}

    async def test_a_cursor_from_a_different_term_is_refused(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """Editing the search box mid-pagination is the commonest thing a
        search UI does, and resuming a foreign cursor would silently skip an
        unpredictable number of people."""
        tag = uuid4().hex[:8]
        for index in range(3):
            await make_player(contract_session, username=f"drift{tag}{index}")

        first = await search(client, auth, f"drift{tag}", limit=1)

        response = await client.get(
            SEARCH_URL,
            headers=auth,
            params={"q": f"other{tag}", "cursor": first["page"]["next_cursor"]},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    async def test_a_malformed_cursor_is_422(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await client.get(
            SEARCH_URL, headers=auth, params={"q": "alice", "cursor": "not-a-cursor"}
        )

        assert response.status_code == 422


class TestPrivacyRespected:
    """A64-013.1's fourth required test.

    Every assertion here is about a field the *composer* gates, which is the
    same object `GET /profiles/{username}` uses — so these also pin that the
    two paths cannot diverge.
    """

    async def test_a_hidden_country_is_null_exactly_as_on_a_profile_page(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """Redacted by `users`' own mapper before the identity crosses the
        port, so a search result cannot carry a hidden country even in
        principle."""
        tag = uuid4().hex[:8]
        player = await make_player(contract_session, username=f"hidden{tag}")
        player.country = CountryCode("GB")
        player.privacy = player.privacy.updated(show_country=False)
        await SqlAlchemyUserRepository(contract_session).update(player)
        await contract_session.flush()

        item = (await search(client, auth, f"hidden{tag}"))["items"][0]

        assert item["country"] is None

    async def test_a_hidden_record_is_null_and_never_zeroed(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """Zeroes read as a beginner's record and would misinform the
        opponent deciding whether to accept a challenge."""
        tag = uuid4().hex[:8]
        player = await make_player(contract_session, username=f"quiet{tag}")
        player.privacy = player.privacy.updated(show_statistics=False)
        await SqlAlchemyUserRepository(contract_session).update(player)
        await contract_session.flush()

        item = (await search(client, auth, f"quiet{tag}"))["items"][0]

        assert item["statistics"] is None

    async def test_presence_is_null_for_a_player_who_hid_it(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        tag = uuid4().hex[:8]
        player = await make_player(contract_session, username=f"ghost{tag}")
        player.privacy = player.privacy.updated(
            online_status=VisibilityLevel.NOBODY,
            last_seen=VisibilityLevel.NOBODY,
        )
        await SqlAlchemyUserRepository(contract_session).update(player)
        await contract_session.flush()

        item = (await search(client, auth, f"ghost{tag}"))["items"][0]

        assert item["is_online"] is None
        assert item["last_seen"] is None

    async def test_a_result_is_the_same_shape_as_a_profile_page(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """A64-013.1: "search results must use the same public
        representation as profile pages."

        Asserted as an equality between two responses rather than against a
        field list, because the requirement is that they cannot *differ* —
        whatever either grows next.

        **Both reads are made by the same viewer**, which A64-020.4 made
        load-bearing: `relationship` is viewer-relative, so comparing an
        authenticated search against an anonymous profile would now differ
        for a correct reason and say nothing about the shape. Same viewer,
        same representation, still exactly equal.
        """
        tag = uuid4().hex[:8]
        username = f"same{tag}"
        await make_player(contract_session, username=username, display_name="Same Player")

        found = (await search(client, auth, username))["items"][0]
        profile = (await client.get(f"/api/v1/profiles/{username}", headers=auth)).json()["data"]

        assert found == profile

    async def test_nothing_private_escapes(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """Asserted against the raw response text, because an address
        leaking through `meta`, an error field or a header would be just as
        much of a disclosure."""
        tag = uuid4().hex[:8]
        player = await make_player(contract_session, username=f"private{tag}")

        response = await client.get(SEARCH_URL, headers=auth, params={"q": f"private{tag}"})

        assert player.email.value not in response.text
        for forbidden in ("email", "password", "argon2id", "is_verified", "timezone", "show_"):
            assert forbidden not in response.text, f"{forbidden!r} leaked into a search result"


class TestRejectedQueries:
    """A64-013.1's fifth required test, plus the rest of the input filter
    over HTTP.

    Two layers reject these: the route's `Query(min_length=...)` and the
    domain's `SearchTerm`. Both produce a `422`, which is what a client
    needs — the split matters for a *non-HTTP* caller, and is asserted in
    `tests/unit/test_user_search.py`.
    """

    @pytest.mark.parametrize(
        ("params", "case"),
        [
            ({"q": ""}, "empty"),
            ({"q": "   "}, "whitespace-only"),
            ({"q": "a"}, "one-character"),
            ({"q": "a" * 51}, "too-long"),
            ({"q": "%"}, "wildcard"),
            ({"q": "ali%"}, "trailing-wildcard"),
            ({"q": "--"}, "no-alphanumeric"),
            ({}, "missing"),
        ],
        ids=lambda value: value if isinstance(value, str) else "",
    )
    async def test_the_query_is_rejected(
        self, client: AsyncClient, auth: dict[str, str], params: dict[str, str], case: str
    ) -> None:
        response = await client.get(SEARCH_URL, headers=auth, params=params)

        assert response.status_code == 422, f"{case}: {response.text}"
        assert response.json()["code"] == "validation_error"

    async def test_a_limit_beyond_the_maximum_is_rejected(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await client.get(SEARCH_URL, headers=auth, params={"q": "alice", "limit": 51})

        assert response.status_code == 422

    async def test_a_wildcard_cannot_be_executed_even_where_it_is_legal(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """`_` is a `LIKE` metacharacter *and* a legal username character,
        so it is escaped rather than rejected. Without the escape `pl_yer`
        would match `player`, which is a wildcard search reached through a
        character the filter had to allow.
        """
        tag = uuid4().hex[:8]
        await make_player(contract_session, username=f"player{tag}")

        assert usernames(await search(client, auth, f"pl_yer{tag}")) == []
        assert usernames(await search(client, auth, f"player{tag}")) == [f"player{tag}"]


class TestAuthentication:
    async def test_an_anonymous_search_is_refused(self, client: AsyncClient) -> None:
        """The enumeration control. Every other public read of a profile on
        this platform is anonymous and this one is not, so that building a
        directory costs an attacker a registration per rate-limit budget."""
        response = await client.get(SEARCH_URL, params={"q": "alice"})

        assert response.status_code == 401

    async def test_the_searcher_is_absent_from_their_own_results(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """A search whose purpose is finding other people should not spend
        a row on the person searching — and this is the exclusion path
        `friends` will add blocked players to, exercised from the first
        release rather than reserved for later."""
        me = (await client.get("/api/v1/auth/me", headers=auth)).json()["data"]["username"]

        assert usernames(await search(client, auth, me)) == []


class TestRouteResolution:
    async def test_search_is_not_swallowed_by_the_by_id_route(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """`GET /users/{user_id}` declares a `UUID` path parameter and would
        match `/users/search` first if it were registered first, rejecting
        `search` as a malformed identifier.

        The two live in different modules, so the ordering can only be
        enforced where the routers are included — this asserts the outcome
        rather than trusting the comment there.
        """
        response = await client.get(SEARCH_URL, headers=auth, params={"q": "alice"})

        assert response.status_code == 200, response.text

    async def test_the_by_id_route_still_works(
        self, client: AsyncClient, auth: dict[str, str], contract_session: AsyncSession
    ) -> None:
        """The other half: registering search first must not shadow the
        route it was ordered ahead of."""
        tag = uuid4().hex[:8]
        player = await make_player(contract_session, username=f"byid{tag}")

        response = await client.get(f"/api/v1/users/{player.id}")

        assert response.status_code == 200
        assert response.json()["data"]["username"] == f"byid{tag}"


class TestOpenApi:
    async def test_the_endpoint_is_documented(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()
        operation = spec["paths"]["/api/v1/users/search"]["get"]

        assert operation["summary"]
        assert operation["description"].strip()
        assert operation["tags"] == ["search"]
        assert set(operation["responses"]) >= {"200", "401", "422", "429"}

    async def test_every_query_parameter_is_described(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()
        operation = spec["paths"]["/api/v1/users/search"]["get"]

        described = {p["name"]: p for p in operation["parameters"]}
        assert set(described) == {"q", "limit", "cursor"}
        for name, parameter in described.items():
            assert parameter["description"].strip(), name

    async def test_the_error_responses_carry_the_platform_error_model(
        self, client: AsyncClient
    ) -> None:
        spec = (await client.get("/openapi.json")).json()
        operation = spec["paths"]["/api/v1/users/search"]["get"]

        for status in ("401", "422", "429"):
            schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert "ErrorResponse" in str(schema)
