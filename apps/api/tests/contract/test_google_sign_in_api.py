""" "Continue with Google", end to end over HTTP — A64-031.E1, ADR-008.

Real PostgreSQL, real Redis, the production routes, cookies and session
machinery. Only Google is replaced: `_FakeGoogle` stands where
`GoogleOidcVerifier` would, accepting one code and asserting one identity —
the verifier's own checks are `tests/unit/test_google_oidc.py`'s. So this
proves the parts no unit test can: that a Google sign-in ends in an
ordinary browser session (same cookie, same refresh rotation, same device
list, same sign-out), that the in-between state survives real Redis, and
that every failure becomes a redirect rather than a page of JSON.

Real Google is **not** exercised here or anywhere in CI; see
`docs/05-operations/google-sign-in.md` for the manual check.

Skipped, not failed, when PostgreSQL or Redis is unreachable.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_settings_dependency
from app.config.settings import GoogleSignInSettings, get_settings
from app.modules.auth.application.services import GoogleSignInService
from app.modules.auth.application.services.opaque_tokens import OpaqueTokenService
from app.modules.auth.domain.exceptions import GoogleSignInRejected
from app.modules.auth.domain.google_sign_in import GoogleIdentity, SignInFailure
from app.modules.auth.infrastructure.external_sign_in_store import RedisExternalSignInStore
from app.modules.auth.presentation.dependencies import (
    AuthenticationServiceDep,
    ExternalIdentityAccountsDep,
    PasswordHasherDep,
    UserCredentialStoreDep,
    get_google_sign_in_service,
)
from app.modules.users.infrastructure.models import UserExternalIdentityModel, UserModel
from tests.contract.contract_app import build_contract_app, contract_client

BROWSER = "/api/v1/auth/browser"
START_URL = f"{BROWSER}/google/start"
CALLBACK_URL = f"{BROWSER}/google/callback"
PENDING_URL = f"{BROWSER}/google/pending"
ACCOUNT_URL = f"{BROWSER}/google/account"
LINK_URL = f"{BROWSER}/google/link"
REFRESH_URL = f"{BROWSER}/refresh"
LOGOUT_URL = f"{BROWSER}/logout"
SESSIONS_URL = f"{BROWSER}/sessions"
REGISTER_URL = f"{BROWSER}/register"
REFRESH_COOKIE = "arena64_refresh"
PENDING_COOKIE = "arena64_google_pending"
STATE_COOKIE = "arena64_google_state"
VALID_CODE = "4/0A-the-one-good-code"
PASSWORD = "CorrectHorse1!"
APP = get_settings().app.public_url


@dataclass
class _FakeGoogle:
    """Accepts `VALID_CODE` and asserts `identity`; refuses anything else
    as `GoogleOidcVerifier` would."""

    identity: GoogleIdentity
    exchanged: list[str] = field(default_factory=list)

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        return f"https://accounts.google.test/auth?state={state}&code_challenge={code_challenge}"

    async def verify(self, *, code: str, code_verifier: str, nonce: str) -> GoogleIdentity:
        self.exchanged.append(code)
        if code != VALID_CODE:
            raise GoogleSignInRejected(SignInFailure.FAILED, "refused")
        return self.identity


def _identity(email: str = "newcomer@gmail.com", **overrides: object) -> GoogleIdentity:
    values: dict[str, object] = {
        "subject": "110169484474386276334",
        "email": email,
        "email_verified": True,
    }
    values.update(overrides)
    return GoogleIdentity(**values)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def google() -> _FakeGoogle:
    return _FakeGoogle(_identity())


@pytest_asyncio.fixture
async def client(
    contract_session: AsyncSession, contract_redis: Redis, google: _FakeGoogle
) -> AsyncIterator[httpx.AsyncClient]:
    app = build_contract_app(contract_session)

    def _service(
        accounts: ExternalIdentityAccountsDep,
        credentials: UserCredentialStoreDep,
        authentication: AuthenticationServiceDep,
        password_hasher: PasswordHasherDep,
    ) -> GoogleSignInService:
        return GoogleSignInService(
            verifier=google,
            store=RedisExternalSignInStore(contract_redis),
            accounts=accounts,
            credentials=credentials,
            authentication=authentication,
            password_hasher=password_hasher,
            tokens=OpaqueTokenService(),
            state_ttl_seconds=600,
            pending_ttl_seconds=900,
        )

    app.dependency_overrides[get_google_sign_in_service] = _service
    async with contract_client(app) as http:
        yield http


async def _round_trip(
    client: httpx.AsyncClient, *, next_path: str = "/play", code: str = VALID_CODE
) -> httpx.Response:
    started = await client.get(START_URL, params={"next": next_path})
    assert started.status_code == 302, started.text
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    return await client.get(CALLBACK_URL, params={"state": state, "code": code})


async def _create_account(client: httpx.AsyncClient, username: str = "new_comer") -> httpx.Response:
    called_back = await _round_trip(client)
    assert called_back.headers["location"] == f"{APP}/register/google"
    return await client.post(ACCOUNT_URL, json={"username": username})


def _cookie_header(response: httpx.Response, name: str) -> str:
    (header,) = [
        value for value in response.headers.get_list("set-cookie") if value.startswith(f"{name}=")
    ]
    return header


class TestANewPlayer:
    async def test_chooses_a_username_and_is_signed_in(
        self, client: httpx.AsyncClient, contract_session: AsyncSession
    ) -> None:
        called_back = await _round_trip(client)
        assert called_back.status_code == 302
        assert called_back.headers["location"] == f"{APP}/register/google"
        assert PENDING_COOKIE in client.cookies

        pending = await client.get(PENDING_URL)
        assert pending.json()["data"] == {
            "purpose": "create_account",
            "email": "newcomer@gmail.com",
            "next": "/play",
        }

        created = await client.post(ACCOUNT_URL, json={"username": "New_Comer"})

        assert created.status_code == 201, created.text
        body = created.json()["data"]
        assert body["user"]["username"] == "New_Comer"
        assert body["user"]["is_verified"] is True
        assert "refresh_token" not in body
        assert REFRESH_COOKIE in client.cookies
        assert PENDING_COOKIE not in client.cookies
        links = list(await contract_session.scalars(select(UserExternalIdentityModel.subject)))
        assert links == ["110169484474386276334"]

    async def test_a_taken_username_is_a_409_and_the_sign_in_survives(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.post(
            REGISTER_URL,
            json={"username": "wanted", "email": "wanted@example.com", "password": PASSWORD},
        )
        client.cookies.clear()
        await _round_trip(client)

        taken = await client.post(ACCOUNT_URL, json={"username": "WANTED"})
        created = await client.post(ACCOUNT_URL, json={"username": "unwanted"})

        assert (taken.status_code, taken.json()["code"]) == (409, "username_already_exists")
        assert created.status_code == 201

    async def test_without_a_pending_sign_in_there_is_nothing_to_create(
        self, client: httpx.AsyncClient
    ) -> None:
        refused = await client.post(ACCOUNT_URL, json={"username": "nobody_here"})

        assert (refused.status_code, refused.json()["code"]) == (401, "external_sign_in_expired")


class TestTheSessionAGoogleSignInGets:
    """The same session a password sign-in gets — rotation, the device
    list, and sign-out all behave identically."""

    async def test_refreshes_lists_and_signs_out_like_any_other(
        self, client: httpx.AsyncClient
    ) -> None:
        created = await _create_account(client)
        access = created.json()["data"]["access_token"]

        rotated = await client.post(REFRESH_URL)
        devices = await client.get(SESSIONS_URL, headers={"Authorization": f"Bearer {access}"})
        signed_out = await client.post(LOGOUT_URL)
        after = await client.post(REFRESH_URL)

        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["data"]["user"]["username"] == "new_comer"
        assert [device["is_current"] for device in devices.json()["data"]] == [True]
        assert signed_out.status_code == 204
        assert after.status_code == 401

    async def test_a_returning_player_lands_where_they_were_going_with_a_cookie(
        self, client: httpx.AsyncClient
    ) -> None:
        await _create_account(client)
        client.cookies.clear()

        returned = await _round_trip(client, next_path="/tournaments/7")

        assert returned.headers["location"] == f"{APP}/tournaments/7"
        cookie = _cookie_header(returned, REFRESH_COOKIE)
        assert "HttpOnly" in cookie
        refreshed = await client.post(REFRESH_URL)
        assert refreshed.json()["data"]["user"]["username"] == "new_comer"

    async def test_no_credential_ever_appears_in_a_redirect(
        self, client: httpx.AsyncClient
    ) -> None:
        await _create_account(client)
        client.cookies.clear()

        returned = await _round_trip(client)

        location = returned.headers["location"]
        assert client.cookies[REFRESH_COOKIE] not in location
        assert "token" not in location
        assert returned.headers["cache-control"] == "no-store"


class TestAnAddressAnAccountAlreadyUses:
    @pytest_asyncio.fixture
    async def existing(self, client: httpx.AsyncClient, google: _FakeGoogle) -> str:
        registered = await client.post(
            REGISTER_URL,
            json={"username": "has_password", "email": "taken@gmail.com", "password": PASSWORD},
        )
        assert registered.status_code == 201, registered.text
        client.cookies.clear()
        google.identity = _identity(email="taken@gmail.com")
        return str(registered.json()["data"]["user"]["id"])

    async def test_is_asked_for_the_password_and_never_signed_in_on_the_address(
        self, client: httpx.AsyncClient, existing: str, contract_session: AsyncSession
    ) -> None:
        called_back = await _round_trip(client)

        assert called_back.headers["location"] == f"{APP}/connect-google"
        assert REFRESH_COOKIE not in client.cookies
        assert (await client.get(PENDING_URL)).json()["data"]["purpose"] == "link_account"
        assert list(await contract_session.scalars(select(UserExternalIdentityModel))) == []

    async def test_a_wrong_password_links_nothing(
        self, client: httpx.AsyncClient, existing: str, contract_session: AsyncSession
    ) -> None:
        await _round_trip(client)

        refused = await client.post(LINK_URL, json={"password": "WrongHorse9?"})

        assert (refused.status_code, refused.json()["code"]) == (401, "invalid_credentials")
        assert list(await contract_session.scalars(select(UserExternalIdentityModel))) == []

    async def test_the_right_password_links_and_signs_in_to_that_account(
        self, client: httpx.AsyncClient, existing: str
    ) -> None:
        await _round_trip(client)

        linked = await client.post(LINK_URL, json={"password": PASSWORD})
        client.cookies.clear()
        returned = await _round_trip(client)

        assert linked.status_code == 200, linked.text
        assert linked.json()["data"]["user"]["id"] == existing
        assert returned.headers["location"] == f"{APP}/play"


class TestACallbackThatSignsNobodyIn:
    async def test_a_replayed_state_is_expired(self, client: httpx.AsyncClient) -> None:
        started = await client.get(START_URL)
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        await client.get(CALLBACK_URL, params={"state": state, "code": VALID_CODE})

        replayed = await client.get(CALLBACK_URL, params={"state": state, "code": VALID_CODE})

        assert replayed.headers["location"] == f"{APP}/login?google=expired"

    async def test_a_state_carried_to_another_browser_is_expired(
        self, client: httpx.AsyncClient, contract_session: AsyncSession
    ) -> None:
        started = await client.get(START_URL)
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        client.cookies.clear()  # the victim's browser holds no binding cookie

        forced = await client.get(CALLBACK_URL, params={"state": state, "code": VALID_CODE})

        assert forced.headers["location"] == f"{APP}/login?google=expired"
        assert list(await contract_session.scalars(select(UserModel))) == []

    async def test_cancelling_at_google_is_cancelled(self, client: httpx.AsyncClient) -> None:
        cancelled = await client.get(CALLBACK_URL, params={"error": "access_denied", "state": "x"})

        assert cancelled.headers["location"] == f"{APP}/login?google=cancelled"

    async def test_a_code_google_refuses_is_failed(
        self, client: httpx.AsyncClient, contract_session: AsyncSession
    ) -> None:
        refused = await _round_trip(client, code="forged")

        assert refused.headers["location"] == f"{APP}/login?google=failed"
        assert list(await contract_session.scalars(select(UserModel))) == []

    async def test_an_unverified_address_is_refused(
        self, client: httpx.AsyncClient, google: _FakeGoogle
    ) -> None:
        google.identity = _identity(email_verified=False)

        refused = await _round_trip(client)

        assert refused.headers["location"] == f"{APP}/login?google=email_unverified"

    async def test_the_binding_cookie_is_cleared_whatever_happened(
        self, client: httpx.AsyncClient
    ) -> None:
        refused = await _round_trip(client, code="forged")

        assert STATE_COOKIE not in client.cookies
        assert "Max-Age=0" in _cookie_header(refused, STATE_COOKIE)


class TestTheCookies:
    async def test_are_httponly_lax_and_scoped_to_the_google_routes(
        self, client: httpx.AsyncClient
    ) -> None:
        started = await client.get(START_URL)
        called_back = await client.get(
            CALLBACK_URL,
            params={
                "state": parse_qs(urlsplit(started.headers["location"]).query)["state"][0],
                "code": VALID_CODE,
            },
        )

        for header in (
            _cookie_header(started, STATE_COOKIE),
            _cookie_header(called_back, PENDING_COOKIE),
        ):
            assert "HttpOnly" in header
            assert "Path=/api/v1/auth/browser/google" in header
            assert "samesite=lax" in header.lower()


class TestAnUnconfiguredDeployment:
    async def test_offers_nothing_and_sends_a_start_back(
        self, contract_session: AsyncSession
    ) -> None:
        # `None` is what `get_google_sign_in_service` resolves to without a
        # client pair (asserted directly in the unit suite); overridden here
        # only because the contract app has no Redis pools for it to reach.
        app = build_contract_app(contract_session)
        app.dependency_overrides[get_google_sign_in_service] = lambda: None
        async with contract_client(app) as client:
            providers = await client.get(f"{BROWSER}/providers")
            started = await client.get(START_URL)

        assert providers.json()["data"] == {"google": False}
        assert started.headers["location"] == f"{APP}/login?google=unavailable"

    async def test_a_configured_one_says_so(self, contract_session: AsyncSession) -> None:
        configured = get_settings().model_copy(
            update={
                "google_sign_in": GoogleSignInSettings.model_validate(
                    {"client_id": "id.apps.googleusercontent.com", "client_secret": "secret"}
                )
            }
        )
        app = build_contract_app(contract_session)
        app.dependency_overrides[get_settings_dependency] = lambda: configured
        async with contract_client(app) as client:
            providers = await client.get(f"{BROWSER}/providers")

        assert providers.json()["data"] == {"google": True}
