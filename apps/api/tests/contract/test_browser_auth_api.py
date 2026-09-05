"""The browser session surface, against real PostgreSQL — A64-020.2.

Two tests, and both are about the properties a *browser* depends on that no
service test can see: what reaches the `Set-Cookie` header, what does not
reach the response body, and what the server does when a cookie arrives
from somewhere it does not recognise.

Deliberately **not** re-tested here: credential verification, rotation
semantics, reuse detection, rate limiting, verification mail. Those are
`test_auth_api.py`'s and the services' own, and this surface calls exactly
the same services — duplicating them would grow the suite without covering
anything new.

Skipped, not failed, when PostgreSQL is unreachable.
"""

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_settings_dependency
from app.config.settings import get_settings
from tests.contract.contract_app import build_contract_app, contract_client

REGISTER_URL = "/api/v1/auth/browser/register"
LOGIN_URL = "/api/v1/auth/browser/login"
REFRESH_URL = "/api/v1/auth/browser/refresh"
LOGOUT_URL = "/api/v1/auth/browser/logout"

COOKIE_NAME = "arena64_refresh"
PASSWORD = "CorrectHorse1!"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def _register(client: AsyncClient, suffix: str) -> dict[str, str]:
    created = await client.post(
        REGISTER_URL,
        json={
            "username": f"player{suffix}",
            "email": f"{suffix}@example.com",
            "password": PASSWORD,
        },
    )
    assert created.status_code == 201, created.text
    return {"email": f"{suffix}@example.com", "password": PASSWORD}


class TestBrowserSession:
    async def test_a_browser_sign_in_returns_no_refresh_token_and_sets_an_httponly_cookie(
        self, client: AsyncClient
    ) -> None:
        """§1, §2 — the property the whole surface exists for.

        The refresh token must reach the browser in exactly one place: a
        cookie the page cannot read. So this asserts both halves, because
        either alone is worthless — a cookie beside a body field is a body
        field, and a page that can read the credential can leak it.

        `HttpOnly` is the security property. `Path` and `SameSite` bound
        where the browser will send it; `Path` in particular has to match
        what `logout` deletes with, or the cookie becomes undeletable.
        """
        credentials = await _register(client, "browsersignin")
        # The registration response is checked for the same property: it
        # signs the browser in, so it is a second place the token could leak.
        assert COOKIE_NAME in client.cookies

        client.cookies.clear()
        signed_in = await client.post(LOGIN_URL, json=credentials)

        assert signed_in.status_code == 200, signed_in.text
        body = signed_in.json()["data"]
        assert "refresh_token" not in body
        assert body["token_type"] == "Bearer"
        assert body["access_token"]
        assert body["expires_in"] > 0
        # The account travels with the session, so a page needs no second
        # call to know who it signed in.
        assert body["user"]["email"] == credentials["email"]

        cookie = signed_in.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "HttpOnly" in cookie
        assert "Path=/api/v1/auth/browser" in cookie
        assert "SameSite=lax" in cookie.lower().replace("samesite=lax", "SameSite=lax")
        # The access token is never a cookie — it belongs in memory, and a
        # second cookie would be a second thing to expire and revoke.
        assert body["access_token"] not in cookie

    async def test_refresh_rotates_the_cookie_and_refuses_an_unrecognised_origin(
        self, contract_session: AsyncSession
    ) -> None:
        """§2, §4 — rotation through the cookie, and the CSRF half.

        **Rotation**: the response carries a new cookie and a new access
        token, and the *old* cookie stops working. That last assertion is
        the one worth having — a refresh that issued a new token without
        invalidating the old one would look identical until a captured
        token was replayed.

        **CSRF**: with trusted origins configured, a request whose `Origin`
        is not one of them is refused **before** the session is touched.
        Asserted after the rotation, so the session is known-good and a
        `403` can only be the origin check.

        The origin list is empty in `local`/`test` — the Vite proxy makes
        the app same-origin, so there is no cross-origin case to allow —
        and `Settings` refuses to start a deployed tier without one. This
        test configures it explicitly to exercise the deployed behaviour.
        """
        async with contract_client(build_contract_app(contract_session)) as client:
            await _register(client, "browserrotate")
            first = client.cookies[COOKIE_NAME]

            rotated = await client.post(REFRESH_URL)

            assert rotated.status_code == 200, rotated.text
            assert rotated.json()["data"]["access_token"]
            assert "refresh_token" not in rotated.json()["data"]
            second = client.cookies[COOKIE_NAME]
            assert second != first

            # The superseded cookie is refused — and A64-028.2 changed
            # *how*. It used to be indistinguishable from a replay, so it
            # was one: `401`, and the chain went with it. That is the defect
            # A64-028.1 measured, because a browser shares one cookie jar
            # and a second tab presents exactly this.
            #
            # It is distinguishable, from three facts the row already
            # carries: revoked by *rotation*, moments ago, and the family
            # still has a live session. So the answer is `409` with a retry
            # hint, no credential, and — the assertion that matters — the
            # session the other tab is holding is still there.
            replay = await client.post(REFRESH_URL, cookies={COOKIE_NAME: first})
            assert replay.status_code == 409, replay.text
            assert replay.json()["code"] == "session_rotation_conflict"
            assert replay.headers["retry-after"]

            still_signed_in = await client.post(REFRESH_URL)
            assert still_signed_in.status_code == 200, still_signed_in.text

        # --- the CSRF half, on a deployment that names its front ends ---
        # Overridden through `Depends`, the way every other varied setting
        # on this platform is — `app.state` belongs to `lifespan`, which a
        # contract app never runs.
        settings = get_settings()
        deployed = build_contract_app(contract_session)
        deployed.dependency_overrides[get_settings_dependency] = lambda: settings.model_copy(
            update={
                "browser_session": settings.browser_session.model_copy(
                    update={"trusted_origins": ("https://arena64.example",)}
                )
            }
        )
        async with contract_client(deployed) as client:
            await _register(client, "browserorigin")

            allowed = await client.post(REFRESH_URL, headers={"Origin": "https://arena64.example"})
            forged = await client.post(
                REFRESH_URL, headers={"Origin": "https://arena64.example.evil.com"}
            )
            absent = await client.post(REFRESH_URL)

            assert allowed.status_code == 200, allowed.text
            # A suffix that would pass a `startswith` check. The origin is
            # rebuilt from parsed parts precisely so this cannot.
            assert forged.status_code == 403, forged.text
            assert forged.json()["code"] == "permission_denied"
            # No echo of either origin — a refusal that named the trusted
            # list would be a way to enumerate the deployment's front ends.
            assert "arena64.example" not in forged.text
            assert absent.status_code == 403, absent.text
