"""The rate-limiting vocabulary and the HTTP layer's subject resolution.

Pure functions and value objects — no Redis, no app. What the real
limiter *does* with these is `tests/contract/test_rate_limiter.py`'s
subject, because atomicity and window behaviour are properties of Lua and
Redis rather than of Python.

What this file is really for is the two places a rate limiter is
silently bypassable, both of which are one-line mistakes that no
end-to-end test would notice:

  **Subject normalisation.** If `Player@Example.com` and
  `player@example.com` land in different buckets, every per-email limit on
  the platform is one shift key away from being doubled — and the only way
  to see it is to vary the case on purpose.

  **`X-Forwarded-For` trust.** If the header is believed without a proxy
  in front, any client sets its own rate-limit identity and the per-IP
  limits are decorative.
"""

from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.api.rate_limiting import (
    UNKNOWN_CLIENT,
    client_ip,
    resolve_subjects,
)
from app.config.settings import RateLimitSettings
from app.core.rate_limiting import (
    KEY_PREFIX,
    KEY_VERSION,
    RateLimitDecision,
    RateLimitRule,
    RateLimitScope,
    RateLimitSubject,
    subject_digest,
)

IP_RULE = RateLimitRule(
    name="login_ip", scope=RateLimitScope.IP, limit=5, window=timedelta(minutes=15)
)
EMAIL_RULE = RateLimitRule(
    name="login_email", scope=RateLimitScope.EMAIL, limit=10, window=timedelta(hours=1)
)


def make_request(
    *,
    body: bytes = b"{}",
    client: tuple[str, int] | None = ("203.0.113.7", 51000),
    headers: dict[str, str] | None = None,
) -> Request:
    """A Starlette `Request` over a canned scope.

    Built by hand rather than through a TestClient because these functions
    are pure and testing them through HTTP would only prove that FastAPI
    routes.
    """
    raw_headers = [(b"content-type", b"application/json")]
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "raw_path": b"/api/v1/auth/login",
            "query_string": b"",
            "headers": raw_headers,
            "client": client,
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )
    # Priming the cache is exactly what `request.json()` does on a real
    # request; setting it here avoids needing a live receive channel.
    request._body = body  # noqa: SLF001 — the documented way to prime a test Request
    return request


class TestRateLimitRule:
    def test_rejects_a_limit_of_zero(self) -> None:
        """A zero is almost always an unset environment variable, and it
        would take the endpoint down completely. Failing at construction
        makes it a startup failure rather than a total outage (DI-06)."""
        with pytest.raises(ValueError, match="at least one request"):
            RateLimitRule(
                name="broken", scope=RateLimitScope.IP, limit=0, window=timedelta(minutes=1)
            )

    def test_rejects_a_negative_limit(self) -> None:
        with pytest.raises(ValueError, match="at least one request"):
            RateLimitRule(
                name="broken", scope=RateLimitScope.IP, limit=-1, window=timedelta(minutes=1)
            )

    def test_rejects_a_zero_window(self) -> None:
        with pytest.raises(ValueError, match="positive window"):
            RateLimitRule(name="broken", scope=RateLimitScope.IP, limit=5, window=timedelta(0))

    def test_window_ms_matches_the_window(self) -> None:
        assert IP_RULE.window_ms == 15 * 60 * 1000

    def test_is_frozen(self) -> None:
        """A rule is configuration. A mutable limit is one an endpoint
        could lower for itself."""
        with pytest.raises(Exception, match="frozen|immutable|assign"):
            IP_RULE.limit = 500  # type: ignore[misc]


class TestSubjectDigest:
    def test_is_stable_across_calls(self) -> None:
        """Unsalted on purpose: every API instance must derive the same key
        for the same caller, or the limit is per-instance rather than
        per-platform."""
        assert subject_digest("player@example.com") == subject_digest("player@example.com")

    def test_folds_case(self) -> None:
        """**The bypass this exists to close.** Without the fold, changing
        one letter's case doubles every per-email allowance on the
        platform."""
        assert subject_digest("Player@Example.COM") == subject_digest("player@example.com")

    def test_strips_surrounding_whitespace(self) -> None:
        assert subject_digest("  player@example.com  ") == subject_digest("player@example.com")

    def test_distinguishes_different_subjects(self) -> None:
        assert subject_digest("a@example.com") != subject_digest("b@example.com")

    def test_does_not_contain_the_subject(self) -> None:
        """A Redis keyspace is readable in bulk by anything holding a
        connection, and has neither the access control nor the retention
        policy the database has (§14.1)."""
        digest = subject_digest("player@example.com")

        assert "player" not in digest
        assert "example.com" not in digest

    def test_is_hex_and_bounded(self) -> None:
        digest = subject_digest("player@example.com")

        assert len(digest) == 32
        assert all(character in "0123456789abcdef" for character in digest)


class TestSubjectKey:
    def test_carries_the_prefix_version_and_rule(self) -> None:
        key = RateLimitSubject(IP_RULE, "203.0.113.7").key

        assert key.startswith(f"{KEY_PREFIX}:{KEY_VERSION}:login_ip:")

    def test_never_contains_the_raw_subject(self) -> None:
        key = RateLimitSubject(EMAIL_RULE, "player@example.com").key

        assert "player@example.com" not in key

    def test_two_rules_over_one_subject_are_two_buckets(self) -> None:
        """Otherwise one endpoint's traffic consumes another's allowance —
        a bug that presents as "sometimes login is limited and nobody knows
        why"."""
        subject = "203.0.113.7"
        other = RateLimitRule(
            name="register_ip", scope=RateLimitScope.IP, limit=3, window=timedelta(hours=1)
        )

        assert RateLimitSubject(IP_RULE, subject).key != RateLimitSubject(other, subject).key

    def test_two_subjects_under_one_rule_are_two_buckets(self) -> None:
        assert (
            RateLimitSubject(IP_RULE, "203.0.113.7").key
            != RateLimitSubject(IP_RULE, "198.51.100.4").key
        )


class TestRetryAfterSeconds:
    def test_rounds_up(self) -> None:
        """Rounding down advertises an instant at which the request is
        still refused, so a client obeying the header exactly is rejected
        again and reasonably concludes the header lies."""
        decision = RateLimitDecision(
            allowed=False, rule=IP_RULE, remaining=0, reset_after=timedelta(milliseconds=1500)
        )

        assert decision.retry_after_seconds == 2

    def test_is_never_zero(self) -> None:
        """Zero reads as "retry immediately", which is the opposite of the
        instruction."""
        decision = RateLimitDecision(
            allowed=False, rule=IP_RULE, remaining=0, reset_after=timedelta(0)
        )

        assert decision.retry_after_seconds == 1

    def test_is_exact_for_a_whole_number_of_seconds(self) -> None:
        decision = RateLimitDecision(
            allowed=False, rule=IP_RULE, remaining=0, reset_after=timedelta(seconds=42)
        )

        assert decision.retry_after_seconds == 42


class TestClientIp:
    def test_uses_the_socket_peer_by_default(self) -> None:
        request = make_request(client=("203.0.113.7", 51000))

        assert client_ip(request, trusted_proxy_count=0) == "203.0.113.7"

    def test_ignores_forwarded_for_when_no_proxy_is_trusted(self) -> None:
        """**The bypass this exists to close.** A header any client can set
        is not an identity; believing it without a proxy in front is a rate
        limiter with an off switch."""
        request = make_request(
            client=("203.0.113.7", 51000),
            headers={"X-Forwarded-For": "1.1.1.1"},
        )

        assert client_ip(request, trusted_proxy_count=0) == "203.0.113.7"

    def test_a_spoofed_chain_cannot_shift_the_identity(self) -> None:
        """With one trusted proxy the address is taken one from the right,
        so entries the caller injected on the left are never read — which
        is what makes the count unspoofable rather than merely awkward to
        spoof."""
        request = make_request(
            client=("10.0.0.1", 51000),
            headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8, 203.0.113.7"},
        )

        assert client_ip(request, trusted_proxy_count=1) == "203.0.113.7"

    def test_reads_the_client_position_behind_one_proxy(self) -> None:
        request = make_request(
            client=("10.0.0.1", 51000),
            headers={"X-Forwarded-For": "203.0.113.7"},
        )

        assert client_ip(request, trusted_proxy_count=1) == "203.0.113.7"

    def test_reads_the_client_position_behind_two_proxies(self) -> None:
        request = make_request(
            client=("10.0.0.1", 51000),
            headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.9"},
        )

        assert client_ip(request, trusted_proxy_count=2) == "203.0.113.7"

    def test_falls_back_to_the_peer_when_the_chain_is_too_short(self) -> None:
        """A header with fewer entries than the trusted chain cannot have
        been written by that chain, so it is discarded entirely rather than
        partially believed."""
        request = make_request(
            client=("10.0.0.1", 51000),
            headers={"X-Forwarded-For": "203.0.113.7"},
        )

        assert client_ip(request, trusted_proxy_count=3) == "10.0.0.1"

    def test_falls_back_to_the_peer_when_the_header_is_absent(self) -> None:
        request = make_request(client=("10.0.0.1", 51000))

        assert client_ip(request, trusted_proxy_count=2) == "10.0.0.1"

    def test_tolerates_whitespace_in_the_chain(self) -> None:
        request = make_request(
            client=("10.0.0.1", 51000),
            headers={"X-Forwarded-For": "  203.0.113.7 ,  10.0.0.9  "},
        )

        assert client_ip(request, trusted_proxy_count=2) == "203.0.113.7"

    def test_an_unknown_peer_shares_one_bucket(self) -> None:
        """The strict choice. Treating an unresolvable peer as exempt would
        make "arrive without an address" a bypass."""
        request = make_request(client=None)

        assert client_ip(request, trusted_proxy_count=0) == UNKNOWN_CLIENT


class TestResolveSubjects:
    settings = RateLimitSettings()

    async def test_binds_an_ip_rule_to_the_caller(self) -> None:
        request = make_request(client=("203.0.113.7", 1))

        subjects = await resolve_subjects(request, [IP_RULE], settings=self.settings)

        assert [subject.subject for subject in subjects] == ["203.0.113.7"]

    async def test_binds_an_email_rule_to_the_body(self) -> None:
        request = make_request(body=b'{"email": "player@example.com", "password": "x"}')

        subjects = await resolve_subjects(request, [EMAIL_RULE], settings=self.settings)

        assert [subject.subject for subject in subjects] == ["player@example.com"]

    async def test_binds_both_rules_on_login(self) -> None:
        request = make_request(body=b'{"email": "player@example.com"}', client=("203.0.113.7", 1))

        subjects = await resolve_subjects(request, [IP_RULE, EMAIL_RULE], settings=self.settings)

        assert [subject.rule.name for subject in subjects] == ["login_ip", "login_email"]

    async def test_never_puts_the_password_in_a_subject(self) -> None:
        """The body carries one on four of the six limited endpoints, and a
        subject becomes a Redis key."""
        request = make_request(body=b'{"email": "a@b.com", "password": "CorrectHorse1!"}')

        subjects = await resolve_subjects(request, [IP_RULE, EMAIL_RULE], settings=self.settings)

        assert all("CorrectHorse1!" not in subject.subject for subject in subjects)
        assert all("CorrectHorse1!" not in subject.key for subject in subjects)

    async def test_drops_an_email_rule_when_the_body_has_no_email(self) -> None:
        """Not a failure. The request is about to 422, and its per-IP rule
        still applies — which is what keeps a flood of malformed bodies
        from being free."""
        request = make_request(body=b'{"username": "nobody"}', client=("203.0.113.7", 1))

        subjects = await resolve_subjects(request, [IP_RULE, EMAIL_RULE], settings=self.settings)

        assert [subject.rule.name for subject in subjects] == ["login_ip"]

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b"not json at all", id="malformed"),
            pytest.param(b"[1, 2, 3]", id="json array"),
            pytest.param(b'"a string"', id="json scalar"),
            pytest.param(b'{"email": 12345}', id="email is a number"),
            pytest.param(b'{"email": null}', id="email is null"),
            pytest.param(b'{"email": "   "}', id="email is blank"),
            pytest.param(b"", id="empty body"),
        ],
    )
    async def test_a_body_that_cannot_yield_an_email_is_not_an_error(self, body: bytes) -> None:
        """Every one of these is about to be rejected by Pydantic with a
        422. This function's only job is to avoid turning that into a
        500 on the way."""
        request = make_request(body=body, client=("203.0.113.7", 1))

        subjects = await resolve_subjects(request, [IP_RULE, EMAIL_RULE], settings=self.settings)

        assert [subject.rule.name for subject in subjects] == ["login_ip"]

    async def test_returns_nothing_when_only_an_email_rule_applies_and_none_is_present(
        self,
    ) -> None:
        request = make_request(body=b"garbage")

        assert await resolve_subjects(request, [EMAIL_RULE], settings=self.settings) == []

    async def test_reading_the_body_leaves_it_readable(self) -> None:
        """The endpoint's own model parses the same bytes afterwards.
        `request.json()` caches the raw body, and that caching is what
        makes reading it in a dependency safe at all."""
        request = make_request(body=b'{"email": "player@example.com"}')

        await resolve_subjects(request, [EMAIL_RULE], settings=self.settings)

        assert await request.json() == {"email": "player@example.com"}

    async def test_honours_the_trusted_proxy_count(self) -> None:
        request = make_request(
            client=("10.0.0.1", 1), headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.9"}
        )
        settings = RateLimitSettings(trusted_proxy_count=2)

        subjects = await resolve_subjects(request, [IP_RULE], settings=settings)

        assert subjects[0].subject == "203.0.113.7"


class TestDependencyOrdering:
    """Rate limiting must run **before** Pydantic validates the body.

    A property of FastAPI's dependency resolution rather than of this
    codebase, which is exactly why it is asserted: it is load-bearing —
    it is what stops "send garbage quickly" from being a cheaper way to
    probe an endpoint than sending something valid — and it would change
    silently under a FastAPI upgrade.
    """

    def test_a_route_dependency_runs_before_body_validation(self) -> None:
        from pydantic import BaseModel

        seen: list[str] = []

        async def guard(request: Request) -> None:
            seen.append("guard")
            await request.body()

        class Body(BaseModel):
            email: str

        app = FastAPI()

        @app.post("/t", dependencies=[Depends(guard)])
        async def handler(payload: Body) -> dict[str, str]:
            seen.append("handler")
            return {"ok": payload.email}

        with TestClient(app) as client:
            response = client.post("/t", json={"wrong_field": 1})

        assert response.status_code == 422
        assert seen == ["guard"], "the guard must run even when the body is rejected"
