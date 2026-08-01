"""Every list endpoint costs a fixed number of queries — A64-013.8.

The performance audit asked for N+1 opportunities to be found. It found none,
and this file is what keeps it that way: for each list endpoint on the social
platform, the number of statements issued to render a page is asserted to be
**the same for a page of one and a page of four**.

That is the whole property, and it is the right shape for it. Asserting an
exact count would fail on any legitimate change — adding a batched provider,
splitting a query — and would be edited to match rather than investigated.
Asserting that the count does not *grow with the page* fails only when
somebody has introduced a per-row read, which is the defect.

## Why contract-level

An N+1 is a property of the composed graph — router, service, composer,
providers, repositories — and every layer of it has looked correct
individually while the whole issued a query per row. The only place the
question can be asked honestly is against a real database with the real
wiring, counting what actually reached the driver.

Measured with SQLAlchemy's `before_cursor_execute`, which fires per statement
sent — so it counts what PostgreSQL was asked to do rather than what the ORM
intended.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.contract_app import build_contract_app, contract_client

BLOCKS_URL = "/api/v1/blocks"
FRIENDS_URL = "/api/v1/friends"
REQUESTS_URL = f"{FRIENDS_URL}/requests"
SEARCH_URL = "/api/v1/users/search"
USERS_URL = "/api/v1/users"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"

#: One and four. Four rather than forty because an N+1 is visible at the
#: first extra row — a larger page would make the suite slower without making
#: the assertion stronger, and this one registers an account per peer.
PAGE_SIZES = (1, 4)


class Player:
    def __init__(self, player_id: UUID, username: str, auth: dict[str, str]) -> None:
        self.id = player_id
        self.username = username
        self.auth = auth


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def register(client: AsyncClient) -> Player:
    suffix = uuid4().hex[:8]
    username = f"player{suffix}"
    assert len(username) <= 20, f"test username {username!r} exceeds the platform limit"

    created = await client.post(
        REGISTER_URL,
        json={"username": username, "email": f"{suffix}@example.com", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text

    signed_in = await client.post(
        LOGIN_URL, json={"email": f"{suffix}@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return Player(
        UUID(created.json()["data"]["id"]),
        username,
        {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"},
    )


async def befriend(client: AsyncClient, a: Player, b: Player) -> None:
    sent = await client.post(REQUESTS_URL, headers=a.auth, json={"player_id": str(b.id)})
    assert sent.status_code == 201, sent.text
    accepted = await client.post(
        f"{REQUESTS_URL}/{sent.json()['data']['id']}/accept", headers=b.auth
    )
    assert accepted.status_code == 200, accepted.text


async def statements_for(
    session: AsyncSession, request: Callable[[], Coroutine[Any, Any, Any]]
) -> int:
    """How many statements one request sends. Counted at the driver."""
    counted: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        counted.append(statement)

    engine = session.get_bind().engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await request()
        assert response.status_code == 200, response.text
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(counted)


@pytest_asyncio.fixture
async def page_costs(
    client: AsyncClient, contract_session: AsyncSession
) -> dict[str, dict[int, int]]:
    """Statement counts for every list endpoint, at each page size.

    Built once and shared by the tests below, because the expensive part is
    *creating* the accounts: a per-test fixture would register fourteen of
    them five times over for assertions that all read the same numbers.
    """
    costs: dict[str, dict[int, int]] = {}

    for size in PAGE_SIZES:
        viewer = await register(client)
        for _ in range(size):
            await befriend(client, viewer, await register(client))
            requester = await register(client)
            sent = await client.post(
                REQUESTS_URL, headers=requester.auth, json={"player_id": str(viewer.id)}
            )
            assert sent.status_code == 201, sent.text
            blocked = await register(client)
            placed = await client.post(
                BLOCKS_URL, headers=viewer.auth, json={"player_id": str(blocked.id)}
            )
            assert placed.status_code == 201, placed.text

        # `auth=viewer.auth` binds the header dict at definition time. The
        # lambdas are invoked in this same iteration, so late binding happens
        # to be harmless today — but a closure over a loop variable is a bug
        # waiting for somebody to collect the callables and run them later,
        # which is exactly what a "measure every endpoint" helper invites.
        auth = viewer.auth
        endpoints: dict[str, Callable[[], Coroutine[Any, Any, Any]]] = {
            "GET /friends": lambda a=auth: client.get(FRIENDS_URL, headers=a),
            "GET /friends/requests/incoming": lambda a=auth: client.get(
                f"{REQUESTS_URL}/incoming", headers=a
            ),
            "GET /blocks": lambda a=auth: client.get(BLOCKS_URL, headers=a),
            "GET /users/search": lambda a=auth: client.get(
                SEARCH_URL, headers=a, params={"q": "player"}
            ),
            "GET /users": lambda: client.get(USERS_URL),
        }
        for name, call in endpoints.items():
            costs.setdefault(name, {})[size] = await statements_for(contract_session, call)

    return costs


@pytest.mark.parametrize(
    "endpoint",
    [
        "GET /friends",
        "GET /friends/requests/incoming",
        "GET /blocks",
        "GET /users/search",
        "GET /users",
    ],
)
async def test_the_query_count_does_not_grow_with_the_page(
    page_costs: dict[str, dict[int, int]], endpoint: str
) -> None:
    """The N+1 guard.

    Each of these composes a page of players through `compose_many`, which
    batches identity, statistics, presence and relationship resolution. A
    change that reverted any one of them to a per-player read would show up
    here as a count that scales with the page and nowhere else — the
    responses would stay byte-identical.
    """
    small, large = (page_costs[endpoint][size] for size in PAGE_SIZES)

    assert small == large, (
        f"{endpoint} issued {small} statements for a page of {PAGE_SIZES[0]} "
        f"and {large} for a page of {PAGE_SIZES[1]} — the count grows with the "
        "page, which is an N+1"
    )


async def test_every_list_endpoint_is_measured(page_costs: dict[str, dict[int, int]]) -> None:
    """The parametrised list above and the fixture must not drift apart.

    A new list endpoint added to the fixture without a parameter here would
    be measured and never asserted on, which is the quietest way for this
    file to stop protecting something.
    """
    assert set(page_costs) == {
        "GET /friends",
        "GET /friends/requests/incoming",
        "GET /blocks",
        "GET /users/search",
        "GET /users",
    }
