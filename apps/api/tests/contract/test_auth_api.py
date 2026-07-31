"""The authentication API end to end — real PostgreSQL, real Argon2id,
real JWTs, real SHA-256.

`tests/unit/test_login_api.py` covers login's HTTP surface with fakes and
`tests/unit/test_session_service.py` covers the orchestration. This file
covers the thing neither can: that the six endpoints compose into a flow
that actually works.

That distinction matters more here than anywhere else on the platform.
Each half of this API was built against a stub of the other — registration
hashes behind a port, login verifies behind the same port, the access
token is minted by one service and validated by another, and the refresh
token is written by a repository and read back by a different code path.
Every one of those pairs was independently green while the pair itself
was never exercised. A wrong `Location` prefix, a token the validator
rejects, a rotated session the endpoint cannot find — none of them shows
up until the calls run in order against real storage.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).

**No `dependency_overrides` anywhere in this file.** The app is the real
one, wired through the real composition root, with only the database
session redirected into the test's rolled-back transaction. Overriding a
service here would put the thing under test behind a fake.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.app_factory import create_app

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
LOGOUT_ALL_URL = "/api/v1/auth/logout-all"
ME_URL = "/api/v1/auth/me"

PASSWORD = "CorrectHorse1!"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app, with only the session redirected.

    Everything else — the hasher, the token provider, the repositories,
    the composition root — is what ships. `contract_session` runs inside
    an outer transaction that is always rolled back, so a test can call
    endpoints that commit without leaving anything behind.

    **`httpx.AsyncClient` over ASGI rather than `fastapi.TestClient`**, and
    the reason is the same event-loop trap `conftest.py` documents for the
    engine. `TestClient` runs the app in a portal with its *own* loop,
    while `contract_session`'s asyncpg connection is bound to the loop
    pytest-asyncio gave this test — observed directly here as
    `RuntimeError: got Future attached to a different loop` on every
    request that touched the database. Driving the app in-process on the
    test's own loop removes the mismatch entirely.

    The ASGI transport does not run the lifespan, which is correct here:
    the only startup state a route needs is the database session, and
    that is exactly what is being overridden.
    """
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield contract_session

    app.dependency_overrides[get_db_session] = _session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


def credentials() -> dict[str, str]:
    """A unique account per call — the tests share one database and the
    unique constraints are real."""
    suffix = uuid4().hex[:10]
    return {
        "username": f"player{suffix}",
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }


async def register(client: AsyncClient, body: dict[str, str] | None = None) -> dict[str, Any]:
    response = await client.post(REGISTER_URL, json=body or credentials())
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def sign_in(client: AsyncClient, body: dict[str, str]) -> dict[str, Any]:
    response = await client.post(
        LOGIN_URL, json={"email": body["email"], "password": body["password"]}
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


class TestRegistration:
    async def test_creates_an_account(self, client: AsyncClient) -> None:
        body = credentials()

        created = await register(client, body)

        assert created["username"] == body["username"]
        assert created["email"] == body["email"]
        assert created["is_verified"] is False

    async def test_the_location_header_is_fetchable(self, client: AsyncClient) -> None:
        """Asserting the string alone is how a wrong prefix gets enshrined
        — A64-011.1 shipped `/v1/users/...` without the `/api` mount and
        the test agreed with it. Following the header is the only version
        of this assertion that catches that."""
        response = await client.post(REGISTER_URL, json=credentials())

        followed = await client.get(response.headers["Location"])

        assert followed.status_code == 200
        assert followed.json()["data"]["id"] == response.json()["data"]["id"]

    async def test_issues_no_tokens(self, client: AsyncClient) -> None:
        """Registration proves you can fill in a form, not that you own
        the address. A session here would hand an unverified account a
        30-day credential before A64-011.6 can verify anything."""
        created = await register(client)

        assert "access_token" not in created
        assert "refresh_token" not in created

    async def test_the_password_never_appears_in_the_response(self, client: AsyncClient) -> None:
        response = await client.post(REGISTER_URL, json=credentials())

        assert PASSWORD not in response.text
        assert "password" not in response.json()["data"]

    async def test_a_duplicate_email_is_409(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)

        response = await client.post(REGISTER_URL, json={**credentials(), "email": body["email"]})

        assert response.status_code == 409
        assert response.json()["code"] == "email_already_exists"

    async def test_a_duplicate_username_is_409(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)

        response = await client.post(
            REGISTER_URL, json={**credentials(), "username": body["username"]}
        )

        assert response.status_code == 409
        assert response.json()["code"] == "username_already_exists"

    async def test_a_weak_password_is_422(self, client: AsyncClient) -> None:
        response = await client.post(REGISTER_URL, json={**credentials(), "password": "weak"})

        assert response.status_code == 422
        assert response.json()["code"] in {"weak_password", "validation_error"}


class TestLogin:
    async def test_returns_a_usable_token_pair(self, client: AsyncClient) -> None:
        """The end-to-end assertion neither half's own tests can make:
        registration hashes behind a port and login verifies behind the
        same port, each tested against a stub. This proves the two
        production implementations agree."""
        body = credentials()
        await register(client, body)

        tokens = await sign_in(client, body)

        assert tokens["token_type"] == "Bearer"
        assert tokens["expires_in"] == 900
        assert (await client.get(ME_URL, headers=bearer(tokens))).status_code == 200

    async def test_the_access_token_is_a_jwt_for_this_account(self, client: AsyncClient) -> None:
        body = credentials()
        created = await register(client, body)

        tokens = await sign_in(client, body)

        assert tokens["access_token"].count(".") == 2
        assert (await client.get(ME_URL, headers=bearer(tokens))).json()["data"]["id"] == created[
            "id"
        ]

    async def test_a_wrong_password_is_401(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)

        response = await client.post(
            LOGIN_URL, json={"email": body["email"], "password": "WrongHorse9?"}
        )

        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"

    async def test_an_unknown_address_fails_identically(self, client: AsyncClient) -> None:
        """The account-enumeration guard, through the real endpoint."""
        body = credentials()
        await register(client, body)

        wrong = await client.post(
            LOGIN_URL, json={"email": body["email"], "password": "WrongHorse9?"}
        )
        unknown = await client.post(
            LOGIN_URL, json={"email": "nobody@example.com", "password": "WrongHorse9?"}
        )

        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["code"] == unknown.json()["code"]
        assert wrong.json()["message"] == unknown.json()["message"]

    async def test_creates_a_session_row(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        body = credentials()
        created = await register(client, body)

        await sign_in(client, body)

        count = (
            await contract_session.execute(
                text("SELECT count(*) FROM auth.user_sessions WHERE user_id = :id"),
                {"id": created["id"]},
            )
        ).scalar()
        assert count == 1

    async def test_two_sign_ins_are_independent_devices(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)

        laptop = await sign_in(client, body)
        phone = await sign_in(client, body)

        assert laptop["refresh_token"] != phone["refresh_token"]
        assert (
            await client.post(LOGOUT_URL, json={"refresh_token": laptop["refresh_token"]})
        ).status_code == 204
        # The phone is untouched.
        assert (
            await client.post(REFRESH_URL, json={"refresh_token": phone["refresh_token"]})
        ).status_code == 200


class TestRefresh:
    async def test_returns_a_new_pair(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)

        response = await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})

        assert response.status_code == 200
        refreshed = response.json()["data"]
        assert refreshed["refresh_token"] != tokens["refresh_token"]
        assert (await client.get(ME_URL, headers=bearer(refreshed))).status_code == 200

    async def test_the_old_refresh_token_stops_working(self, client: AsyncClient) -> None:
        """Rotation on every use — database.md §14.3."""
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)
        await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})

        replay = await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})

        assert replay.status_code == 401
        assert replay.json()["code"] == "invalid_session"

    async def test_replaying_burns_the_whole_chain(self, client: AsyncClient) -> None:
        """The reason rotation exists. A token used twice was captured, and
        since the platform cannot tell the attacker from the legitimate
        user, both lose the session."""
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)
        successor = (
            await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})
        ).json()["data"]

        await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})

        burned = await client.post(REFRESH_URL, json={"refresh_token": successor["refresh_token"]})
        assert burned.status_code == 401

    async def test_a_chain_of_refreshes_works(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)
        token = (await sign_in(client, body))["refresh_token"]

        for _ in range(5):
            response = await client.post(REFRESH_URL, json={"refresh_token": token})
            assert response.status_code == 200, response.text
            token = response.json()["data"]["refresh_token"]

        assert (await client.post(REFRESH_URL, json={"refresh_token": token})).status_code == 200

    async def test_refreshing_leaves_one_live_session(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """A rotation replaces rather than accumulates — five refreshes
        must not leave five live sessions in a device list."""
        body = credentials()
        created = await register(client, body)
        token = (await sign_in(client, body))["refresh_token"]
        for _ in range(4):
            token = (await client.post(REFRESH_URL, json={"refresh_token": token})).json()["data"][
                "refresh_token"
            ]

        live = (
            await contract_session.execute(
                text(
                    "SELECT count(*) FROM auth.user_sessions "
                    "WHERE user_id = :id AND revoked_at IS NULL"
                ),
                {"id": created["id"]},
            )
        ).scalar()
        assert live == 1

    async def test_an_unknown_token_is_401(self, client: AsyncClient) -> None:
        response = await client.post(REFRESH_URL, json={"refresh_token": "never-issued"})

        assert response.status_code == 401
        assert response.json()["code"] == "invalid_session"

    async def test_an_empty_token_is_422(self, client: AsyncClient) -> None:
        assert (await client.post(REFRESH_URL, json={"refresh_token": ""})).status_code == 422

    async def test_a_revoked_session_cannot_refresh(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)
        await client.post(LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]})

        response = await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})

        assert response.status_code == 401


class TestLogout:
    async def test_returns_204(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)

        response = await client.post(LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]})

        assert response.status_code == 204
        assert response.content == b""

    async def test_is_idempotent(self, client: AsyncClient) -> None:
        """A retry after a dropped response must not error — the caller
        wanted the state it is already in."""
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)
        await client.post(LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]})

        assert (
            await client.post(LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]})
        ).status_code == 204

    async def test_does_not_burn_the_chain_on_a_repeat(self, client: AsyncClient) -> None:
        """Signing out twice is not an attack. Routing logout through the
        reuse-detecting validator would make a double-clicked button look
        like a replay and log a security alert for it."""
        body = credentials()
        await register(client, body)
        laptop = await sign_in(client, body)
        phone = await sign_in(client, body)
        await client.post(LOGOUT_URL, json={"refresh_token": laptop["refresh_token"]})
        await client.post(LOGOUT_URL, json={"refresh_token": laptop["refresh_token"]})

        assert (
            await client.post(REFRESH_URL, json={"refresh_token": phone["refresh_token"]})
        ).status_code == 200

    async def test_an_unknown_token_is_401(self, client: AsyncClient) -> None:
        """The one case where "you are signed out" would be a lie."""
        response = await client.post(LOGOUT_URL, json={"refresh_token": "never-issued"})

        assert response.status_code == 401

    async def test_the_access_token_still_works_until_it_expires(self, client: AsyncClient) -> None:
        """The documented cost of a stateless token: nothing can revoke it
        for its remaining minutes. Asserted so the gap is a recorded
        property rather than a surprise — closing it needs the `jti`
        denylist recommended for A64-011.6."""
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)

        await client.post(LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]})

        assert (await client.get(ME_URL, headers=bearer(tokens))).status_code == 200


class TestLogoutAll:
    async def test_revokes_every_session(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)
        laptop = await sign_in(client, body)
        phone = await sign_in(client, body)

        response = await client.post(LOGOUT_ALL_URL, headers=bearer(laptop))

        assert response.status_code == 204
        for tokens in (laptop, phone):
            assert (
                await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})
            ).status_code == 401

    async def test_revokes_the_calling_session_too(self, client: AsyncClient) -> None:
        """ "Log out everywhere" that quietly excluded the device you asked
        from is not what anyone means by it."""
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)

        await client.post(LOGOUT_ALL_URL, headers=bearer(tokens))

        assert (
            await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})
        ).status_code == 401

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.post(LOGOUT_ALL_URL)

        assert response.status_code == 401
        assert response.json()["code"] == "authentication_required"

    async def test_does_not_touch_another_account(self, client: AsyncClient) -> None:
        mine = credentials()
        theirs = credentials()
        await register(client, mine)
        await register(client, theirs)
        my_tokens = await sign_in(client, mine)
        their_tokens = await sign_in(client, theirs)

        await client.post(LOGOUT_ALL_URL, headers=bearer(my_tokens))

        assert (
            await client.post(REFRESH_URL, json={"refresh_token": their_tokens["refresh_token"]})
        ).status_code == 200

    async def test_is_idempotent(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)
        await client.post(LOGOUT_ALL_URL, headers=bearer(tokens))

        assert (await client.post(LOGOUT_ALL_URL, headers=bearer(tokens))).status_code == 204


class TestCurrentUser:
    async def test_returns_the_callers_own_account(self, client: AsyncClient) -> None:
        body = credentials()
        created = await register(client, body)
        tokens = await sign_in(client, body)

        response = await client.get(ME_URL, headers=bearer(tokens))

        assert response.status_code == 200
        assert response.json()["data"]["id"] == created["id"]
        assert response.json()["data"]["email"] == body["email"]

    async def test_carries_no_password_hash(self, client: AsyncClient) -> None:
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)

        response = await client.get(ME_URL, headers=bearer(tokens))

        assert "password" not in response.text
        assert "argon2" not in response.text

    async def test_without_a_token_is_401(self, client: AsyncClient) -> None:
        response = await client.get(ME_URL)

        assert response.status_code == 401
        assert response.json()["code"] == "authentication_required"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    async def test_with_a_forged_token_is_401(self, client: AsyncClient) -> None:
        response = await client.get(ME_URL, headers={"Authorization": "Bearer not.a.token"})

        assert response.status_code == 401
        assert response.json()["code"] == "invalid_token"

    async def test_a_refresh_token_is_not_accepted_as_an_access_token(
        self, client: AsyncClient
    ) -> None:
        """The `type` claim doing its job. A refresh token presented as a
        bearer credential must be refused — otherwise its 30-day lifetime
        becomes the access window."""
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)

        response = await client.get(
            ME_URL, headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
        )

        assert response.status_code == 401

    async def test_reflects_the_account_after_a_refresh(self, client: AsyncClient) -> None:
        body = credentials()
        created = await register(client, body)
        tokens = await sign_in(client, body)
        refreshed = (
            await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})
        ).json()["data"]

        response = await client.get(ME_URL, headers=bearer(refreshed))

        assert response.json()["data"]["id"] == created["id"]


class TestCredentialsNeverLeak:
    async def test_no_endpoint_echoes_the_password(self, client: AsyncClient) -> None:
        body = credentials()
        responses = [
            await client.post(REGISTER_URL, json=body),
            await client.post(LOGIN_URL, json={"email": body["email"], "password": PASSWORD}),
            await client.post(LOGIN_URL, json={"email": body["email"], "password": "Wrong1!"}),
            await client.post(REGISTER_URL, json=body),
        ]

        for response in responses:
            assert PASSWORD not in response.text

    async def test_the_refresh_token_is_not_stored_in_recoverable_form(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§14.3: "the token itself exists only in transit and in the
        client". A database read must not yield a working credential."""
        body = credentials()
        await register(client, body)
        tokens = await sign_in(client, body)

        hit = (
            await contract_session.execute(
                text(
                    "SELECT count(*) FROM auth.user_sessions "
                    "WHERE encode(refresh_token_hash, 'escape') LIKE :fragment"
                ),
                {"fragment": f"%{tokens['refresh_token'][:12]}%"},
            )
        ).scalar()
        assert hit == 0
