"""The published viewer relationship, against real PostgreSQL — A64-020.4.

`ProfileResponse.relationship` is what a client renders a social button
from, so what matters is that it is **exactly one state, always the right
one, and never one that discloses something**. All three need real rows in
three relations, which is why this is a contract suite and not a unit test.

Deliberately not re-tested here: friendship, request and block *mechanics*.
Those are `test_friends_api.py`'s, `test_friend_requests_api.py`'s and
`test_blocking_api.py`'s. This asserts only what the new field says about
them.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.contract_app import build_contract_app, contract_client

SEARCH_URL = "/api/v1/users/search"
REQUESTS_URL = "/api/v1/friends/requests"
BLOCKS_URL = "/api/v1/blocks"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"


class Player:
    def __init__(self, player_id: UUID, username: str, auth: dict[str, str]) -> None:
        self.id = player_id
        self.username = username
        self.auth = auth


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def register(client: AsyncClient, session: AsyncSession) -> Player:
    suffix = uuid4().hex[:10]
    username = f"rel{suffix}"
    created = await client.post(
        REGISTER_URL,
        json={"username": username, "email": f"{suffix}@example.com", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text

    # **Verified**, because A64-021.5H made every friend-graph write require
    # it. The same thing `app.operator.accounts verify` does; the OTP flow
    # belongs to `test_otp_verification.py`.
    await session.execute(
        text("UPDATE users.user SET is_verified = true WHERE id = :id"),
        {"id": UUID(created.json()["data"]["id"])},
    )

    signed_in = await client.post(
        LOGIN_URL, json={"email": f"{suffix}@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return Player(
        UUID(created.json()["data"]["id"]),
        username,
        {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"},
    )


async def profile_of(client: AsyncClient, target: Player, viewer: Player | None) -> Any:
    headers = viewer.auth if viewer is not None else {}
    response = await client.get(f"/api/v1/profiles/{target.username}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def send_request(client: AsyncClient, sender: Player, target: Player) -> str:
    sent = await client.post(REQUESTS_URL, headers=sender.auth, json={"player_id": str(target.id)})
    assert sent.status_code == 201, sent.text
    return str(sent.json()["data"]["id"])


class TestEveryState:
    async def test_search_reports_each_state_in_a_fixed_number_of_statements(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§5, §6 — every state on one page, and no N+1.

        A search page is the hardest case and the reason the field exists:
        it mixes a friend, somebody who asked, somebody who was asked, and
        a stranger, so a per-player resolution would be four queries for
        four rows and forty for forty.

        Asserted by counting statements for a page of one and a page of
        four. Equal counts is the property; the numbers themselves are
        reported rather than pinned, because a legitimate change — a
        batched provider, a split query — should not fail this while an
        introduced per-row read must.
        """
        viewer = await register(client, contract_session)
        friend = await register(client, contract_session)
        asked_me = await register(client, contract_session)
        i_asked = await register(client, contract_session)

        # friend
        request_id = await send_request(client, viewer, friend)
        accepted = await client.post(f"{REQUESTS_URL}/{request_id}/accept", headers=friend.auth)
        assert accepted.status_code == 200, accepted.text
        # incoming, then outgoing
        await send_request(client, asked_me, viewer)
        await send_request(client, viewer, i_asked)

        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = contract_session.get_bind().engine

        async def search(limit: int) -> tuple[list[Any], int]:
            statements.clear()
            event.listen(engine, "before_cursor_execute", record)
            try:
                page = await client.get(
                    SEARCH_URL, headers=viewer.auth, params={"q": "rel", "limit": limit}
                )
            finally:
                event.remove(engine, "before_cursor_execute", record)
            assert page.status_code == 200, page.text
            return page.json()["data"]["items"], len(statements)

        one, cost_of_one = await search(1)
        many, cost_of_many = await search(4)

        assert len(one) == 1
        assert len(many) >= 3

        # The whole point: the same cost for four rows as for one.
        assert cost_of_one == cost_of_many, (
            f"search issued {cost_of_one} statements for one row and "
            f"{cost_of_many} for four — the relationship resolution is per row"
        )

        states = {row["username"]: row["relationship"] for row in many}
        assert states[friend.username] == "friend"
        assert states[asked_me.username] == "incoming_request"
        assert states[i_asked.username] == "outgoing_request"

    async def test_it_follows_every_transition(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§10 — the state after send, cancel, accept, remove, block, unblock.

        One player pair driven through the whole lifecycle, because the
        thing worth asserting is not any single state but that the field
        **tracks** — a value that were right once and stale afterwards would
        render a button that does the wrong thing.
        """
        viewer = await register(client, contract_session)
        other = await register(client, contract_session)

        assert (await profile_of(client, other, viewer))["relationship"] == "none"

        # --- send, then cancel ---
        request_id = await send_request(client, viewer, other)
        assert (await profile_of(client, other, viewer))["relationship"] == "outgoing_request"
        # ...and the same request is `incoming` from the other side. One row,
        # two directions, and the direction is what decides the button.
        assert (await profile_of(client, viewer, other))["relationship"] == "incoming_request"

        cancelled = await client.delete(f"{REQUESTS_URL}/{request_id}", headers=viewer.auth)
        assert cancelled.status_code in (200, 204), cancelled.text
        assert (await profile_of(client, other, viewer))["relationship"] == "none"

        # --- send, then accept ---
        request_id = await send_request(client, viewer, other)
        accepted = await client.post(f"{REQUESTS_URL}/{request_id}/accept", headers=other.auth)
        assert accepted.status_code == 200, accepted.text
        assert (await profile_of(client, other, viewer))["relationship"] == "friend"
        assert (await profile_of(client, viewer, other))["relationship"] == "friend"

        # --- block, which also ends the friendship ---
        blocked = await client.post(
            BLOCKS_URL, headers=viewer.auth, json={"player_id": str(other.id)}
        )
        assert blocked.status_code == 201, blocked.text
        assert (await profile_of(client, other, viewer))["relationship"] == "blocked"

        # --- unblock ---
        unblocked = await client.delete(f"{BLOCKS_URL}/{other.id}", headers=viewer.auth)
        assert unblocked.status_code in (200, 204), unblocked.text
        # Not back to `friend`: blocking ended the friendship, and lifting a
        # block does not restore one. The field says so rather than
        # guessing.
        assert (await profile_of(client, other, viewer))["relationship"] == "none"


class TestWhatItNeverSays:
    async def test_it_is_absent_for_anonymous_and_for_your_own_profile(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§2, §3 — `null`, and specifically not `none`.

        The two mean different things and a client renders different things
        for them: `none` is "signed in, no relationship", which is an
        `Add friend` button, while `null` is "there is nobody to have a
        relationship with", which is no social controls at all.
        """
        viewer = await register(client, contract_session)

        anonymous = await profile_of(client, viewer, None)
        assert anonymous["relationship"] is None

        own = await profile_of(client, viewer, viewer)
        assert own["relationship"] is None

    async def test_being_blocked_is_never_disclosed(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§1, §9 — BL-1, asserted from the blocked player's side.

        The blocker sees `blocked`. The blocked player must see nothing
        that distinguishes their situation from an ordinary absence — and
        the enum has no member that could say it even if a bug tried.

        Search is the assertion that matters: a blocked player looking for
        the blocker simply does not find them, which is indistinguishable
        from the account not existing.
        """
        blocker = await register(client, contract_session)
        target = await register(client, contract_session)

        placed = await client.post(
            BLOCKS_URL, headers=blocker.auth, json={"player_id": str(target.id)}
        )
        assert placed.status_code == 201, placed.text

        # The blocker's own view names it, because it is their own action.
        assert (await profile_of(client, target, blocker))["relationship"] == "blocked"

        # The target's view of the blocker discloses nothing. Whether the
        # profile is reachable at all is the block's own behaviour; what
        # this asserts is that no reachable representation ever says
        # `blocked`.
        seen = await client.get(f"/api/v1/profiles/{blocker.username}", headers=target.auth)
        if seen.status_code == 200:
            assert seen.json()["data"]["relationship"] != "blocked"

        found = await client.get(SEARCH_URL, headers=target.auth, params={"q": blocker.username})
        assert found.status_code == 200, found.text
        assert [row["username"] for row in found.json()["data"]["items"]] == []
