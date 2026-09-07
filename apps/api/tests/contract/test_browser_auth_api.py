"""The browser session surface, against real PostgreSQL — A64-020.2, A64-030.5C.

The first two are about the properties a *browser* depends on that no
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
SESSIONS_URL = "/api/v1/auth/browser/sessions"

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


def _auth(response: object) -> dict[str, str]:
    """The `Authorization` header for a browser sign-in response.

    Both new routes authenticate with the **access token**, exactly as
    `logout-all` does. The cookie is a device label on the list and an
    authorisation on nothing.
    """
    token = response.json()["data"]["access_token"]  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


async def _sign_in(client: AsyncClient, suffix: str, user_agent: str) -> dict[str, str]:
    """Registers, which also signs the browser in, and returns its
    `Authorization` header. The user agent is explicit because httpx sends
    its own, which parses to nothing and would make every label assertion
    vacuous."""
    created = await client.post(
        REGISTER_URL,
        json={
            "username": f"player{suffix}",
            "email": f"{suffix}@example.com",
            "password": PASSWORD,
        },
        headers={"User-Agent": user_agent},
    )
    assert created.status_code == 201, created.text
    return _auth(created)


CHROME_MACOS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"
)


class TestTheDeviceList:
    """`GET`/`DELETE /auth/browser/sessions` — A64-030.5C, SE-2.

    The properties no service test can see: that the cookie decides which
    row is current, that nothing credential-shaped reaches the body, and
    that one account cannot read or revoke another's devices over HTTP.
    """

    async def test_it_lists_this_browser_and_marks_it_current(self, client: AsyncClient) -> None:
        headers = await _sign_in(client, "devicelist", CHROME_MACOS)

        listed = await client.get(SESSIONS_URL, headers=headers)

        assert listed.status_code == 200, listed.text
        [device] = listed.json()["data"]
        assert device["is_current"] is True
        assert (device["browser"], device["platform"]) == ("Chrome", "macOS")

    async def test_the_response_carries_nothing_a_credential_could_hide_in(
        self, client: AsyncClient
    ) -> None:
        """Asserted against the **whole key set**, not by looking for a
        few forbidden names: a field added to the read model later would
        pass a denylist and fail this."""
        headers = await _sign_in(client, "devicesafe", CHROME_MACOS)
        cookie = client.cookies[COOKIE_NAME]

        listed = await client.get(SESSIONS_URL, headers=headers)

        [device] = listed.json()["data"]
        assert set(device) == {
            "id",
            "is_current",
            "browser",
            "platform",
            "signed_in_at",
            "last_active_at",
        }
        # The refresh token is the credential this surface exists to keep
        # out of anything a page can read.
        assert cookie not in listed.text

    async def test_a_second_device_is_another_row_and_is_not_current(
        self, contract_session: AsyncSession
    ) -> None:
        """Two cookie jars, which is what two devices actually are."""
        async with contract_client(build_contract_app(contract_session)) as laptop:
            laptop_auth = await _sign_in(laptop, "twodevices", CHROME_MACOS)

            async with contract_client(build_contract_app(contract_session)) as phone:
                signed_in = await phone.post(
                    LOGIN_URL,
                    json={"email": "twodevices@example.com", "password": PASSWORD},
                    headers={"User-Agent": SAFARI_IPHONE},
                )
                assert signed_in.status_code == 200, signed_in.text
                phone_auth = _auth(signed_in)

                from_phone = (await phone.get(SESSIONS_URL, headers=phone_auth)).json()["data"]

            from_laptop = (await laptop.get(SESSIONS_URL, headers=laptop_auth)).json()["data"]

        assert len(from_laptop) == 2
        # The same two devices, marked differently depending on who asks —
        # which is the whole point of reading the cookie.
        assert {device["platform"] for device in from_laptop} == {"macOS", "iPhone"}
        assert [d["platform"] for d in from_laptop if d["is_current"]] == ["macOS"]
        assert [d["platform"] for d in from_phone if d["is_current"]] == ["iPhone"]

    async def test_rotation_neither_adds_a_row_nor_moves_the_marker(
        self, client: AsyncClient
    ) -> None:
        """The reason a row is a family and not a session.

        A browser refreshes roughly every fifteen minutes. If the list
        counted rows, this would report two devices; if the identifier
        were a row id, the one the page is holding would already be gone.
        """
        headers = await _sign_in(client, "devicerotate", CHROME_MACOS)
        before = (await client.get(SESSIONS_URL, headers=headers)).json()["data"]

        rotated = await client.post(REFRESH_URL)
        assert rotated.status_code == 200, rotated.text

        after = (await client.get(SESSIONS_URL, headers=_auth(rotated))).json()["data"]

        assert len(after) == 1
        assert after[0]["id"] == before[0]["id"]
        assert after[0]["is_current"] is True
        assert after[0]["signed_in_at"] == before[0]["signed_in_at"]

    async def test_it_never_shows_another_accounts_devices(
        self, contract_session: AsyncSession
    ) -> None:
        async with contract_client(build_contract_app(contract_session)) as mine:
            my_auth = await _sign_in(mine, "deviceowner", CHROME_MACOS)

            async with contract_client(build_contract_app(contract_session)) as theirs:
                await _sign_in(theirs, "devicestranger", SAFARI_IPHONE)

            listed = (await mine.get(SESSIONS_URL, headers=my_auth)).json()["data"]

        assert [device["platform"] for device in listed] == ["macOS"]

    async def test_revoking_another_device_signs_it_out(
        self, contract_session: AsyncSession
    ) -> None:
        """The end-to-end property SE-2 exists for: a device somebody does
        not recognise stops working, from a device they do."""
        async with contract_client(build_contract_app(contract_session)) as laptop:
            laptop_auth = await _sign_in(laptop, "devicerevoke", CHROME_MACOS)

            async with contract_client(build_contract_app(contract_session)) as phone:
                signed_in = await phone.post(
                    LOGIN_URL,
                    json={"email": "devicerevoke@example.com", "password": PASSWORD},
                    headers={"User-Agent": SAFARI_IPHONE},
                )
                phone_auth = _auth(signed_in)
                [phone_device] = [
                    device
                    for device in (await phone.get(SESSIONS_URL, headers=phone_auth)).json()["data"]
                    if device["is_current"]
                ]

                revoked = await laptop.delete(
                    f"{SESSIONS_URL}/{phone_device['id']}", headers=laptop_auth
                )
                assert revoked.status_code == 204, revoked.text

                # The phone's cookie is now worthless.
                assert (await phone.post(REFRESH_URL)).status_code == 401

            remaining = (await laptop.get(SESSIONS_URL, headers=laptop_auth)).json()["data"]
            assert [device["platform"] for device in remaining] == ["macOS"]
            # And the laptop that did the revoking is untouched.
            assert (await laptop.post(REFRESH_URL)).status_code == 200

    async def test_another_accounts_device_cannot_be_revoked(
        self, contract_session: AsyncSession
    ) -> None:
        """`404`, and **not** `401`.

        `revoke_family` takes no user id, so without the ownership check
        this call would succeed. The status matters as much as the
        refusal: a `401` would make a browser's unauthorised interceptor
        sign the caller out of their own account.
        """
        async with contract_client(build_contract_app(contract_session)) as attacker:
            attacker_auth = await _sign_in(attacker, "devicethief", CHROME_MACOS)

            async with contract_client(build_contract_app(contract_session)) as victim:
                victim_auth = await _sign_in(victim, "devicevictim", SAFARI_IPHONE)
                [target] = (await victim.get(SESSIONS_URL, headers=victim_auth)).json()["data"]

                refused = await attacker.delete(
                    f"{SESSIONS_URL}/{target['id']}", headers=attacker_auth
                )

                assert refused.status_code == 404, refused.text
                # Still signed in, which is the assertion that matters.
                assert (await victim.post(REFRESH_URL)).status_code == 200

    async def test_revoking_twice_is_still_a_success(self, client: AsyncClient) -> None:
        """A retry after a dropped response is not an error."""
        headers = await _sign_in(client, "deviceretry", CHROME_MACOS)
        [device] = (await client.get(SESSIONS_URL, headers=headers)).json()["data"]

        first = await client.delete(f"{SESSIONS_URL}/{device['id']}", headers=headers)
        second = await client.delete(f"{SESSIONS_URL}/{device['id']}", headers=headers)

        assert (first.status_code, second.status_code) == (204, 204)

    async def test_the_revoke_refuses_an_unrecognised_origin(
        self, contract_session: AsyncSession
    ) -> None:
        """It is a destructive state change on a cookie-bearing surface, so
        it carries the same CSRF check `refresh` and `logout` do. Without
        it, another site could sign a signed-in player out of their own
        devices."""
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
            headers = await _sign_in(client, "deviceorigin", CHROME_MACOS)
            [device] = (await client.get(SESSIONS_URL, headers=headers)).json()["data"]

            forged = await client.delete(
                f"{SESSIONS_URL}/{device['id']}",
                headers={**headers, "Origin": "https://arena64.example.evil.com"},
            )

            assert forged.status_code == 403, forged.text
            assert forged.json()["code"] == "permission_denied"
            # And the session it tried to end is still live. The trusted
            # `Origin` has to be sent here too: with a list configured,
            # `refresh` refuses an absent origin exactly as it refuses a
            # wrong one.
            live = await client.post(REFRESH_URL, headers={"Origin": "https://arena64.example"})
            assert live.status_code == 200, live.text
