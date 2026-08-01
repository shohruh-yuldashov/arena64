"""Rate limiting on the six authentication endpoints, end to end.

Real PostgreSQL, real Redis, real Argon2id, the real composition root —
with only the database session redirected into the test's rolled-back
transaction and the limits lowered so a test can reach them in a few
requests instead of thirty.

`tests/unit/test_auth_rate_limits.py` reads the policy off the routes and
`tests/contract/test_rate_limiter.py` proves the limiter's behaviour. This
file exists for the gap between them, which is where a rate limiter
usually fails: **it is possible for every one of those tests to pass while
no request is ever actually limited.** A guard attached to a route but
resolving its subjects from the wrong place, a limiter built on a Redis
role nothing writes to, a dependency override left in the composition
root — none of those show up until the calls run in order against the
real thing.

So the assertions here are deliberately coarse and behavioural: send
requests until one is refused, and check that the right one was.

## Why the limits are lowered rather than the defaults exercised

`POST /auth/refresh` permits 30 requests a minute. Proving it refuses the
31st means 30 successful token rotations, each a database write, to assert
one thing. The limits arrive through `get_rate_limit_settings`, so
lowering them is an override rather than a fiction — the *mechanism* under
test is identical, and the production numbers are asserted for real in
`test_auth_rate_limits.py`.

Skipped, not failed, when PostgreSQL or Redis is unreachable.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_rate_limit_settings, get_rate_limiter
from app.app_factory import create_app
from app.config.settings import RateLimitSettings
from app.core.clock import SystemClock
from app.database.rate_limiter import RedisRateLimiter

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
FORGOT_URL = "/api/v1/auth/password/forgot"
RESET_URL = "/api/v1/auth/password/reset"
RESEND_URL = "/api/v1/auth/email/resend"
LOGOUT_URL = "/api/v1/auth/logout"

PASSWORD = "CorrectHorse1!"

#: Small enough to reach in a handful of requests, and each still greater
#: than one so "the limit is not simply zero" is visible in the results.
TEST_SETTINGS = RateLimitSettings(
    enabled=True,
    # One trusted proxy, so a test can present a distinct caller address
    # through `X-Forwarded-For` — which is also the only way to prove the
    # per-IP rules are per *IP* rather than per process.
    trusted_proxy_count=1,
    # The per-email limit stays **above** the per-IP one, as it is in
    # production (10 against 5). The ordering is not cosmetic: with the
    # email limit lower, it would bind first on every single-host test and
    # the per-IP rule would never be the one that refused — so a test aimed
    # at the IP rule would silently be testing the email rule instead.
    login_ip_limit=3,
    login_email_limit=5,
    register_ip_limit=2,
    forgot_password_email_limit=2,
    resend_verification_email_limit=2,
    refresh_ip_limit=2,
    password_reset_ip_limit=2,
)


@pytest_asyncio.fixture
async def client(
    contract_session: AsyncSession, contract_redis: Redis
) -> AsyncIterator[AsyncClient]:
    """The production app with a real limiter on a flushed Redis.

    The ASGI transport does not run `lifespan`, so the limiter that
    lifespan builds is supplied here instead — pointed at
    `contract_redis`, which the fixture flushes on both sides so no test
    inherits another's counters.
    """
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield contract_session

    limiter = RedisRateLimiter(contract_redis, settings=TEST_SETTINGS, clock=SystemClock())

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    app.dependency_overrides[get_rate_limit_settings] = lambda: TEST_SETTINGS

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


def caller(address: str) -> dict[str, str]:
    """Presents a distinct client address. `trusted_proxy_count=1` above is
    what makes this believed — without it the header is ignored, which is
    itself asserted in `tests/unit/test_rate_limiting.py`."""
    return {"X-Forwarded-For": address}


def credentials() -> dict[str, str]:
    suffix = uuid4().hex[:10]
    return {
        "username": f"player{suffix}",
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }


async def register(client: AsyncClient, address: str) -> dict[str, str]:
    account = credentials()
    response = await client.post(REGISTER_URL, json=account, headers=caller(address))
    assert response.status_code == 201, response.text
    return account


class TestRegister:
    async def test_refuses_after_the_limit(self, client: AsyncClient) -> None:
        for _ in range(TEST_SETTINGS.register_ip_limit):
            assert (
                await client.post(REGISTER_URL, json=credentials(), headers=caller("203.0.113.1"))
            ).status_code == 201

        refused = await client.post(REGISTER_URL, json=credentials(), headers=caller("203.0.113.1"))

        assert refused.status_code == 429
        assert refused.json()["code"] == "rate_limited"

    async def test_a_different_ip_is_unaffected(self, client: AsyncClient) -> None:
        """**"different IPs".** Mass account creation from one host is
        bounded; a legitimate signup from elsewhere is not."""
        for _ in range(TEST_SETTINGS.register_ip_limit + 2):
            await client.post(REGISTER_URL, json=credentials(), headers=caller("203.0.113.1"))

        elsewhere = await client.post(
            REGISTER_URL, json=credentials(), headers=caller("198.51.100.9")
        )

        assert elsewhere.status_code == 201

    async def test_the_refusal_carries_retry_after(self, client: AsyncClient) -> None:
        for _ in range(TEST_SETTINGS.register_ip_limit + 1):
            response = await client.post(
                REGISTER_URL, json=credentials(), headers=caller("203.0.113.2")
            )

        assert int(response.headers["Retry-After"]) > 0

    async def test_successful_responses_publish_the_budget(self, client: AsyncClient) -> None:
        response = await client.post(
            REGISTER_URL, json=credentials(), headers=caller("203.0.113.3")
        )

        assert response.headers["X-RateLimit-Limit"] == str(TEST_SETTINGS.register_ip_limit)
        assert response.headers["X-RateLimit-Remaining"] == str(TEST_SETTINGS.register_ip_limit - 1)


class TestLogin:
    async def test_brute_force_from_one_host_is_bounded(self, client: AsyncClient) -> None:
        """The per-IP rule. Guessing one account's password from one host
        stops after `login_ip_limit` attempts."""
        account = await register(client, "203.0.113.10")
        body = {"email": account["email"], "password": "WrongHorse9?"}

        for _ in range(TEST_SETTINGS.login_ip_limit):
            assert (
                await client.post(LOGIN_URL, json=body, headers=caller("203.0.113.11"))
            ).status_code == 401

        refused = await client.post(LOGIN_URL, json=body, headers=caller("203.0.113.11"))

        assert refused.status_code == 429

    async def test_a_distributed_attempt_on_one_account_is_bounded(
        self, client: AsyncClient
    ) -> None:
        """**The per-email rule doing the thing per-IP cannot.** Every
        request here comes from a different host, so each gets a fresh
        per-IP allowance — and the attempt is stopped anyway."""
        account = await register(client, "203.0.113.20")
        body = {"email": account["email"], "password": "WrongHorse9?"}

        for index in range(TEST_SETTINGS.login_email_limit):
            response = await client.post(
                LOGIN_URL, json=body, headers=caller(f"198.51.100.{index}")
            )
            assert response.status_code == 401

        refused = await client.post(LOGIN_URL, json=body, headers=caller("198.51.100.200"))

        assert refused.status_code == 429

    async def test_a_different_email_is_unaffected(self, client: AsyncClient) -> None:
        """**"different emails".** One account being attacked must not lock
        every other account out — which is what a limiter keyed on nothing
        but the endpoint would do."""
        victim = await register(client, "203.0.113.30")
        bystander = await register(client, "203.0.113.31")

        for index in range(TEST_SETTINGS.login_email_limit + 2):
            await client.post(
                LOGIN_URL,
                json={"email": victim["email"], "password": "WrongHorse9?"},
                headers=caller(f"198.51.100.{index}"),
            )

        theirs = await client.post(
            LOGIN_URL,
            json={"email": bystander["email"], "password": PASSWORD},
            headers=caller("198.51.100.250"),
        )

        assert theirs.status_code == 200, theirs.text

    async def test_a_successful_sign_in_still_counts(self, client: AsyncClient) -> None:
        """The limit is on *requests*, not on failures. A limiter that only
        counted failures would let an attacker who has already succeeded
        keep going unbounded — and would make the endpoint's cost
        unbounded regardless of outcome."""
        account = await register(client, "203.0.113.40")
        body = {"email": account["email"], "password": PASSWORD}

        for _ in range(TEST_SETTINGS.login_ip_limit):
            assert (
                await client.post(LOGIN_URL, json=body, headers=caller("203.0.113.41"))
            ).status_code == 200

        assert (
            await client.post(LOGIN_URL, json=body, headers=caller("203.0.113.41"))
        ).status_code == 429

    async def test_a_malformed_body_still_consumes_the_ip_allowance(
        self, client: AsyncClient
    ) -> None:
        """The guard runs before Pydantic, so "send garbage quickly" is not
        a cheaper way to probe the endpoint than sending something
        valid."""
        for _ in range(TEST_SETTINGS.login_ip_limit):
            response = await client.post(
                LOGIN_URL, content=b"not json", headers=caller("203.0.113.50")
            )
            assert response.status_code == 422

        refused = await client.post(LOGIN_URL, content=b"not json", headers=caller("203.0.113.50"))

        assert refused.status_code == 429


class TestForgotPassword:
    async def test_mail_bombing_one_inbox_is_bounded(self, client: AsyncClient) -> None:
        account = await register(client, "203.0.113.60")
        body = {"email": account["email"]}

        for index in range(TEST_SETTINGS.forgot_password_email_limit):
            assert (
                await client.post(FORGOT_URL, json=body, headers=caller(f"198.51.100.{index}"))
            ).status_code == 204

        refused = await client.post(FORGOT_URL, json=body, headers=caller("198.51.100.150"))

        assert refused.status_code == 429

    async def test_an_unknown_address_is_limited_the_same_way(self, client: AsyncClient) -> None:
        """**The limiter must not become the enumeration oracle the
        endpoint spends its whole design avoiding.** If only real accounts
        were counted, the *first* 429 would confirm an address exists.
        """
        unknown = {"email": f"{uuid4().hex}@example.com"}

        for _ in range(TEST_SETTINGS.forgot_password_email_limit):
            assert (
                await client.post(FORGOT_URL, json=unknown, headers=caller("203.0.113.70"))
            ).status_code == 204

        refused = await client.post(FORGOT_URL, json=unknown, headers=caller("203.0.113.70"))

        assert refused.status_code == 429

    async def test_a_different_address_is_unaffected(self, client: AsyncClient) -> None:
        first = {"email": f"{uuid4().hex}@example.com"}
        for _ in range(TEST_SETTINGS.forgot_password_email_limit + 1):
            await client.post(FORGOT_URL, json=first, headers=caller("203.0.113.80"))

        second = {"email": f"{uuid4().hex}@example.com"}
        response = await client.post(FORGOT_URL, json=second, headers=caller("203.0.113.80"))

        assert response.status_code == 204


class TestResendVerification:
    async def test_is_bounded_per_address(self, client: AsyncClient) -> None:
        account = await register(client, "203.0.113.90")
        body = {"email": account["email"]}

        for index in range(TEST_SETTINGS.resend_verification_email_limit):
            assert (
                await client.post(RESEND_URL, json=body, headers=caller(f"198.51.100.{index}"))
            ).status_code == 202

        refused = await client.post(RESEND_URL, json=body, headers=caller("198.51.100.160"))

        assert refused.status_code == 429


class TestRefresh:
    async def test_is_bounded_per_host(self, client: AsyncClient) -> None:
        account = await register(client, "203.0.113.100")
        signed_in = await client.post(
            LOGIN_URL,
            json={"email": account["email"], "password": PASSWORD},
            headers=caller("203.0.113.101"),
        )
        tokens: dict[str, Any] = signed_in.json()["data"]

        for _ in range(TEST_SETTINGS.refresh_ip_limit):
            response = await client.post(
                REFRESH_URL,
                json={"refresh_token": tokens["refresh_token"]},
                headers=caller("203.0.113.102"),
            )
            assert response.status_code == 200, response.text
            tokens = response.json()["data"]

        refused = await client.post(
            REFRESH_URL,
            json={"refresh_token": tokens["refresh_token"]},
            headers=caller("203.0.113.102"),
        )

        assert refused.status_code == 429

    async def test_a_refused_refresh_does_not_rotate_the_token(self, client: AsyncClient) -> None:
        """The guard runs before the handler, so a refused request has not
        touched the session. A limiter that rejected *after* rotation would
        leave the client holding a token the server had already
        invalidated — signed out by the rate limiter."""
        account = await register(client, "203.0.113.110")
        signed_in = await client.post(
            LOGIN_URL,
            json={"email": account["email"], "password": PASSWORD},
            headers=caller("203.0.113.111"),
        )
        token = signed_in.json()["data"]["refresh_token"]

        for _ in range(TEST_SETTINGS.refresh_ip_limit + 2):
            await client.post(
                REFRESH_URL, json={"refresh_token": token}, headers=caller("203.0.113.112")
            )

        # The very first refresh consumed the token by rotating it; every
        # later one was refused before reaching the session. Presenting it
        # from an unexhausted host must therefore be reuse detection —
        # a 401 — rather than a success, and must not be a 500.
        replayed = await client.post(
            REFRESH_URL, json={"refresh_token": token}, headers=caller("203.0.113.113")
        )

        assert replayed.status_code == 401


class TestResetPassword:
    async def test_is_bounded_per_host(self, client: AsyncClient) -> None:
        """The endpoint performs an Argon2id hash, which makes it the
        cheapest CPU-amplification primitive in the module."""
        body = {"token": "never-issued-by-anyone", "password": "BrandNewHorse2!"}

        for _ in range(TEST_SETTINGS.password_reset_ip_limit):
            assert (
                await client.post(RESET_URL, json=body, headers=caller("203.0.113.120"))
            ).status_code == 422

        refused = await client.post(RESET_URL, json=body, headers=caller("203.0.113.120"))

        assert refused.status_code == 429


class TestUnlimitedEndpoints:
    async def test_logout_is_not_rate_limited(self, client: AsyncClient) -> None:
        """One of the four endpoints A64-011.8 does not list. Asserted so
        that adding a guard is a deliberate change rather than a silent
        one."""
        account = await register(client, "203.0.113.130")
        signed_in = await client.post(
            LOGIN_URL,
            json={"email": account["email"], "password": PASSWORD},
            headers=caller("203.0.113.131"),
        )
        token = signed_in.json()["data"]["refresh_token"]

        for _ in range(10):
            response = await client.post(
                LOGOUT_URL, json={"refresh_token": token}, headers=caller("203.0.113.132")
            )
            assert response.status_code == 204


class TestKeysInRedis:
    async def test_counters_are_written_to_redis(
        self, client: AsyncClient, contract_redis: Redis
    ) -> None:
        """The wiring assertion this whole file exists for: a guard on a
        route that never reaches storage would pass every behavioural test
        above while counting nothing, as long as the limits were never
        reached."""
        await client.post(REGISTER_URL, json=credentials(), headers=caller("203.0.113.140"))

        assert await contract_redis.keys("rl:v1:register_ip:*")

    async def test_no_key_carries_an_address(
        self, client: AsyncClient, contract_redis: Redis
    ) -> None:
        account = credentials()
        await client.post(REGISTER_URL, json=account, headers=caller("203.0.113.150"))
        await client.post(
            FORGOT_URL, json={"email": account["email"]}, headers=caller("203.0.113.150")
        )

        keys = [key.decode() for key in await contract_redis.keys("*")]
        assert keys
        assert all(account["email"] not in key for key in keys)
        assert all("203.0.113.150" not in key for key in keys)

    async def test_every_key_expires(self, client: AsyncClient, contract_redis: Redis) -> None:
        await client.post(REGISTER_URL, json=credentials(), headers=caller("203.0.113.160"))

        for key in await contract_redis.keys("*"):
            assert await contract_redis.pttl(key) > 0
