"""The HTTP surface of rate limiting — the 429, the headers, the block
log, and what an unreachable Redis does to a request.

In `tests/unit/` per services.md §1 ("no I/O, fakes only"): the limiter is
one of the doubles in `tests/fakes/rate_limiter.py`, and everything else —
routing, the exception handler, the response envelope — is production.

Uses a **purpose-built app** rather than the real `auth` router, and that
is the point rather than a shortcut. Driving these assertions through
`POST /auth/login` would mean standing up a user repository, a hasher and a
session service to reach a code path that has nothing to do with any of
them, and every one of those would be a way for the test to fail for an
unrelated reason. `tests/unit/test_auth_rate_limits.py` asserts that the
real endpoints carry these guards; this file asserts what a guard does.

The one thing that must be real here is the exception handler: the whole
question is what reaches the client, and `TooManyRequests` is rendered by
`app/api/exception_handlers.py` rather than by anything in this file.
"""

import logging
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.deps import get_rate_limit_settings, get_rate_limiter
from app.api.exception_handlers import register_exception_handlers
from app.api.rate_limiting import RateLimit
from app.common.middleware import CorrelationIdMiddleware, RequestIdMiddleware
from app.config.settings import RateLimitSettings
from app.core.rate_limiting import RateLimitRule, RateLimitScope
from tests.fakes.rate_limiter import (
    AllowAllRateLimiter,
    BrokenRateLimiter,
    DenyAllRateLimiter,
)

URL = "/guarded"

IP_RULE = RateLimitRule(
    name="test_ip", scope=RateLimitScope.IP, limit=5, window=timedelta(minutes=15)
)
EMAIL_RULE = RateLimitRule(
    name="test_email", scope=RateLimitScope.EMAIL, limit=10, window=timedelta(hours=1)
)


class Body(BaseModel):
    email: str


def build_app(limiter: object, *, settings: RateLimitSettings | None = None) -> FastAPI:
    """A one-route app carrying the real guard, handlers and middleware.

    The middleware is here because the error envelope carries
    `request_id` and `correlation_id`, and a 429 that omitted them would be
    the one error on the platform a client's parser could not read. Testing
    the envelope without the middleware that fills it would assert a
    weaker contract than production keeps.
    """
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    guard = RateLimit("guarded", lambda _: (IP_RULE, EMAIL_RULE))

    @app.post(URL, dependencies=[Depends(guard)])
    async def handler(payload: Body) -> dict[str, str]:
        return {"email": payload.email}

    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    app.dependency_overrides[get_rate_limit_settings] = lambda: settings or RateLimitSettings()
    return app


@pytest.fixture
def allowed_client() -> Iterator[TestClient]:
    with TestClient(build_app(AllowAllRateLimiter())) as client:
        yield client


@pytest.fixture
def denied_client() -> Iterator[TestClient]:
    with TestClient(build_app(DenyAllRateLimiter(retry_after=timedelta(seconds=42)))) as client:
        yield client


VALID = {"email": "player@example.com"}


class TestAllowedRequests:
    def test_the_request_succeeds(self, allowed_client: TestClient) -> None:
        assert allowed_client.post(URL, json=VALID).status_code == 200

    def test_headers_are_published_before_the_limit_is_reached(
        self, allowed_client: TestClient
    ) -> None:
        """`X-RateLimit-Remaining` is only useful *before* it reaches zero.
        A client that learns its budget exists at the moment it is refused
        cannot pace itself, which is the entire purpose of publishing the
        numbers."""
        response = allowed_client.post(URL, json=VALID)

        assert response.headers["X-RateLimit-Limit"] == "5"
        assert response.headers["X-RateLimit-Remaining"] == "5"
        assert int(response.headers["X-RateLimit-Reset"]) > 0

    def test_no_retry_after_on_a_successful_response(self, allowed_client: TestClient) -> None:
        """`Retry-After` on a 200 tells a well-behaved client to wait for
        a request that already succeeded."""
        assert "retry-after" not in allowed_client.post(URL, json=VALID).headers

    def test_the_headers_describe_the_binding_rule_only(self, allowed_client: TestClient) -> None:
        """Three headers, not six. Publishing every rule would make a
        client implement its own "which one bites first" logic and would
        tell an attacker exactly which dimensions the endpoint counts."""
        headers = allowed_client.post(URL, json=VALID).headers

        assert len([name for name in headers if name.lower().startswith("x-ratelimit")]) == 3


class TestDeniedRequests:
    def test_returns_429(self, denied_client: TestClient) -> None:
        assert denied_client.post(URL, json=VALID).status_code == 429

    def test_carries_the_platform_error_envelope(self, denied_client: TestClient) -> None:
        body = denied_client.post(URL, json=VALID).json()

        assert body["code"] == "rate_limited"
        assert body["message"]
        assert body["request_id"]
        assert body["correlation_id"]

    def test_carries_retry_after(self, denied_client: TestClient) -> None:
        assert denied_client.post(URL, json=VALID).headers["Retry-After"] == "42"

    def test_retry_after_is_delta_seconds_not_a_date(self, denied_client: TestClient) -> None:
        """RFC 9110 §10.2.3 permits both. Delta-seconds does not require
        the client's clock to agree with the server's, and a mobile
        client's often does not."""
        assert denied_client.post(URL, json=VALID).headers["Retry-After"].isdigit()

    def test_carries_the_rate_limit_headers(self, denied_client: TestClient) -> None:
        headers = denied_client.post(URL, json=VALID).headers

        assert headers["X-RateLimit-Limit"] == "5"
        assert headers["X-RateLimit-Remaining"] == "0"
        assert headers["X-RateLimit-Reset"] == "42"

    def test_the_handler_never_runs(self, denied_client: TestClient) -> None:
        """A guard that returned a verdict rather than raising would leave
        this to the endpoint, and the endpoint that forgets is the one
        unprotected while looking protected."""
        assert "email" not in denied_client.post(URL, json=VALID).json()

    def test_the_message_does_not_name_the_rule(self, denied_client: TestClient) -> None:
        """Naming the dimension that refused is the one piece of
        information needed to evade it: "per email" says rotate the
        address, "per IP" says rotate the host."""
        response = denied_client.post(URL, json=VALID)

        assert "test_ip" not in response.text
        assert "test_email" not in response.text
        assert "ip" not in response.json()["message"].lower()

    def test_the_response_never_echoes_the_email(self, denied_client: TestClient) -> None:
        assert "player@example.com" not in denied_client.post(URL, json=VALID).text

    def test_the_reply_is_identical_for_every_caller(self, denied_client: TestClient) -> None:
        """A 429 that differed by whether the address exists would turn the
        limiter into the account-enumeration oracle that
        `/auth/password/forgot` spends its whole design avoiding."""
        known = denied_client.post(URL, json={"email": "player@example.com"})
        unknown = denied_client.post(URL, json={"email": "nobody@example.com"})

        assert known.status_code == unknown.status_code
        assert known.json()["code"] == unknown.json()["code"]
        assert known.json()["message"] == unknown.json()["message"]


class TestBlockLogging:
    def test_logs_a_blocked_request_at_warning(
        self, denied_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """WARNING, not INFO — services.md §7.1 puts "rate limit breached"
        at WARN. A single block is ordinary; the *rate* of them is the only
        signal that a credential-stuffing run is in progress, and a signal
        buried alongside every successful request is not a signal."""
        with caplog.at_level(logging.WARNING):
            denied_client.post(URL, json=VALID)

        record = next(r for r in caplog.records if r.message == "rate_limit_blocked")
        assert record.levelno == logging.WARNING

    def test_records_the_endpoint_the_ip_and_the_rule(
        self, denied_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            denied_client.post(URL, json=VALID)

        record = next(r for r in caplog.records if r.message == "rate_limit_blocked")
        assert record.endpoint == URL  # type: ignore[attr-defined]
        assert record.method == "POST"  # type: ignore[attr-defined]
        assert record.ip  # type: ignore[attr-defined]
        assert record.rule == "test_ip"  # type: ignore[attr-defined]

    def test_never_logs_the_email(
        self, denied_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The rule name says which dimension fired; the value would put an
        address in a log line (services.md §8.5). The blocked party is
        identified by IP, which is what a block list takes."""
        with caplog.at_level(logging.DEBUG):
            denied_client.post(URL, json=VALID)

        assert "player@example.com" not in caplog.text

    def test_never_logs_a_password(
        self, denied_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            denied_client.post(
                URL, json={"email": "player@example.com", "password": "CorrectHorse1!"}
            )

        assert "CorrectHorse1!" not in caplog.text

    def test_an_allowed_request_is_not_logged_as_a_block(
        self, allowed_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            allowed_client.post(URL, json=VALID)

        assert "rate_limit_blocked" not in caplog.text


class TestRedisUnavailable:
    """What an outage of the limiter's own dependency does to a request —
    `RateLimitSettings.fail_open`."""

    def test_fail_open_allows_the_request(self) -> None:
        """A Redis outage degrades abuse prevention rather than removing
        the ability to sign in. Argon2id, `locked_until` and the generic
        sign-in failure are all still in place — this is losing defence in
        depth, not losing the defence."""
        with TestClient(build_app(BrokenRateLimiter(fail_open=True))) as client:
            assert client.post(URL, json=VALID).status_code == 200

    def test_fail_open_still_publishes_headers(self) -> None:
        """Truthful ones: nothing is being counted, so nothing has been
        consumed. Fabricating a falling countdown would let a client — and
        an operator watching a dashboard — conclude the limiter was
        working."""
        with TestClient(build_app(BrokenRateLimiter(fail_open=True))) as client:
            response = client.post(URL, json=VALID)

        assert response.headers["X-RateLimit-Remaining"] == response.headers["X-RateLimit-Limit"]

    def test_fail_closed_returns_503_not_429(self) -> None:
        """The caller did nothing wrong. A 429 would tell them to back off
        for an hour over a fault that may clear in seconds; 503 is both
        true and correctly retryable."""
        with TestClient(build_app(BrokenRateLimiter(fail_open=False))) as client:
            response = client.post(URL, json=VALID)

        assert response.status_code == 503
        assert response.json()["code"] == "transient_infrastructure_error"

    def test_fail_closed_does_not_run_the_handler(self) -> None:
        with TestClient(build_app(BrokenRateLimiter(fail_open=False))) as client:
            assert "email" not in client.post(URL, json=VALID).json()


class TestMalformedRequests:
    def test_a_malformed_body_still_consumes_the_ip_allowance(self) -> None:
        """The ordering property. FastAPI resolves route dependencies
        before validating the body, so "send garbage quickly" is not a
        cheaper way to probe an endpoint than sending something valid."""
        limiter = AllowAllRateLimiter()
        with TestClient(build_app(limiter)) as client:
            response = client.post(URL, content=b"not json at all")

        assert response.status_code == 422
        assert len(limiter.calls) == 1
        assert [subject.rule.name for subject in limiter.calls[0]] == ["test_ip"]

    def test_a_body_with_no_email_still_consumes_the_ip_allowance(self) -> None:
        limiter = AllowAllRateLimiter()
        with TestClient(build_app(limiter)) as client:
            client.post(URL, json={"username": "nobody"})

        assert [subject.rule.name for subject in limiter.calls[0]] == ["test_ip"]

    def test_a_valid_body_consumes_both_allowances(self) -> None:
        limiter = AllowAllRateLimiter()
        with TestClient(build_app(limiter)) as client:
            client.post(URL, json=VALID)

        assert [subject.rule.name for subject in limiter.calls[0]] == ["test_ip", "test_email"]

    def test_a_blocked_malformed_request_is_still_429_not_422(self) -> None:
        """The limit is checked first, so an exhausted caller is refused
        before their body is even parsed — which is the cheap rejection
        the ordering exists to buy."""
        with TestClient(build_app(DenyAllRateLimiter())) as client:
            assert client.post(URL, content=b"not json").status_code == 429


class TestDisabledLimiter:
    def test_the_kill_switch_allows_everything(self) -> None:
        """`RATE_LIMIT_ENABLED=false` is checked inside the adapter, so
        this asserts through a real `RedisRateLimiter` pointed at a Redis
        that does not exist — if the switch were not honoured, the
        unreachable client would be consulted and the call would take the
        full timeout."""
        from redis.asyncio import Redis

        from app.core.clock import SystemClock
        from app.database.rate_limiter import RedisRateLimiter

        settings = RateLimitSettings(enabled=False, fail_open=False)
        limiter = RedisRateLimiter(
            Redis.from_url("redis://invalid.invalid:6379/0"),
            settings=settings,
            clock=SystemClock(),
        )

        with TestClient(build_app(limiter, settings=settings)) as client:
            response = client.post(URL, json=VALID)

        # 200 rather than the 503 `fail_open=False` would have produced.
        assert response.status_code == 200


class TestRuleConfiguration:
    def test_a_guard_with_no_rules_is_refused(self) -> None:
        """A guard resolving to nothing limits nothing while looking like a
        limit — the failure mode a reviewer cannot see in a route
        decorator."""
        guard = RateLimit("empty", lambda _: ())

        with pytest.raises(ValueError, match="no rules"):
            guard.rules(RateLimitSettings())

    def test_rules_are_resolved_from_the_injected_settings(self) -> None:
        """Which is what makes a test able to lower a limit at all — rules
        built at import time would freeze the production numbers."""
        guard = RateLimit(
            "configurable",
            lambda settings: (
                RateLimitRule(
                    name="from_settings",
                    scope=RateLimitScope.IP,
                    limit=settings.login_ip_limit,
                    window=timedelta(seconds=settings.login_ip_window_seconds),
                ),
            ),
        )

        assert guard.rules(RateLimitSettings(login_ip_limit=99))[0].limit == 99
