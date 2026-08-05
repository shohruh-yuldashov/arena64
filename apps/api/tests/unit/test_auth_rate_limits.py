"""`auth`'s rate-limit policy — that the six endpoints A64-011.8 names
carry the limits it specifies.

**Structural, not behavioural, and deliberately so.** Proving that
`POST /auth/login` refuses a sixth attempt by sending six attempts would
take five successful Argon2 verifications, a user repository and a session
service, and would still only prove it for whatever the limit happened to
be that day. `tests/contract/test_rate_limiting_api.py` does exactly one
end-to-end pass to prove the wiring is real; this file reads the policy.

That split also makes this the one suite the global
`RATE_LIMIT_ENABLED=false` in `conftest.py` cannot weaken — nothing here
executes a limiter, so nothing here can be silently switched off. If a
future change removed a guard from a route, these are the assertions that
would fail.
"""

from collections.abc import Iterable
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api.rate_limiting import RateLimit
from app.app_factory import create_app
from app.config.settings import RateLimitSettings
from app.core.rate_limiting import RateLimitScope
from app.modules.auth.presentation.rate_limits import build_rules

#: Every endpoint A64-011.8 lists, with the limits in force. Written out as
#: data rather than derived from `build_rules`, which would make the test
#: agree with the implementation by construction and assert nothing.
#:
#: Two figures are **not** A64-011.8's, and both were raised by A64-020.6:
#: `login_ip` 5 -> 20 and `register_ip` 3 -> 10. An IP is not a user, so the
#: old numbers locked out the sixth person on a shared connection and the
#: fourth person signing up together. The rules that bound an *attack* —
#: `login_email`, Argon2id, `users.locked_until`, email verification — are
#: unchanged, which is the property this table exists to keep visible: a
#: future loosening of `login_email` should look wrong here.
#:
#: `(path, rule name, scope, limit, window seconds)`
EXPECTED: list[tuple[str, str, RateLimitScope, int, int]] = [
    ("/api/v1/auth/login", "login_ip", RateLimitScope.IP, 20, 15 * 60),
    ("/api/v1/auth/login", "login_email", RateLimitScope.EMAIL, 10, 60 * 60),
    ("/api/v1/auth/register", "register_ip", RateLimitScope.IP, 10, 60 * 60),
    (
        "/api/v1/auth/password/forgot",
        "forgot_password_email",
        RateLimitScope.EMAIL,
        3,
        60 * 60,
    ),
    (
        "/api/v1/auth/email/resend",
        "resend_verification_email",
        RateLimitScope.EMAIL,
        3,
        60 * 60,
    ),
    ("/api/v1/auth/refresh", "refresh_ip", RateLimitScope.IP, 30, 60),
    # Listed under "Endpoints" with no limit given — this figure is chosen,
    # and `RateLimitSettings` says so at length.
    ("/api/v1/auth/password/reset", "reset_password_ip", RateLimitScope.IP, 10, 60 * 60),
    # --- the cookie surface, A64-020.2 --------------------------------------
    # The **same three rules** as the bearer-token routes above, deliberately
    # sharing a bucket with them: `/auth/register` and
    # `/auth/browser/register` are two doors into one action, and separate
    # counters would double every allowance for anybody willing to alternate.
    #
    # Absent from this table before A64-020.6, which is how five guarded
    # endpoints went untested for four phases — see the count below.
    ("/api/v1/auth/browser/login", "login_ip", RateLimitScope.IP, 20, 15 * 60),
    ("/api/v1/auth/browser/login", "login_email", RateLimitScope.EMAIL, 10, 60 * 60),
    ("/api/v1/auth/browser/register", "register_ip", RateLimitScope.IP, 10, 60 * 60),
    ("/api/v1/auth/browser/refresh", "refresh_ip", RateLimitScope.IP, 30, 60),
]

LIMITED_PATHS = sorted({path for path, *_ in EXPECTED})


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app()


def api_routes(app: FastAPI) -> list[tuple[str, APIRoute]]:
    """Every `APIRoute` in the app, with its fully-prefixed path.

    Walks nested routers rather than reading `app.routes` directly. This
    FastAPI keeps an included router as one opaque entry carrying an
    `original_router` and the prefix it was mounted at, instead of
    flattening its routes into the parent — so the obvious one-level loop
    finds nothing, and a helper that finds nothing makes every assertion
    below **vacuously pass**. That is the worst outcome available to a file
    whose whole job is to notice a missing guard, which is why
    `test_the_walker_finds_every_auth_route` exists directly below and
    fails loudly if a FastAPI upgrade changes this structure.
    """
    found: list[tuple[str, APIRoute]] = []

    # `Any` rather than `object`: `_IncludedRouter` is private to FastAPI
    # and has no public type to name, so this walk is duck-typed by
    # necessity. The guard against that being wrong is behavioural, not
    # static — see `TestTheWalkerItself`.
    def walk(routes: Iterable[Any], prefix: str) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.append((prefix + route.path, route))
            elif hasattr(route, "original_router"):
                mounted_at = getattr(route.include_context, "prefix", "")
                walk(route.original_router.routes, prefix + mounted_at)
            elif hasattr(route, "routes"):
                walk(route.routes, prefix)

    walk(app.routes, "")
    return found


def guards_on(app: FastAPI, path: str) -> list[RateLimit]:
    """Every `RateLimit` dependency declared on a route.

    Reads FastAPI's resolved dependency tree rather than the source, so a
    guard that was imported but never attached — the exact mistake this
    file exists to catch — is invisible to it.
    """
    found: list[RateLimit] = []
    for route_path, route in api_routes(app):
        if route_path != path:
            continue
        for dependency in route.dependant.dependencies:
            if isinstance(dependency.call, RateLimit):
                found.append(dependency.call)
    return found


class TestTheWalkerItself:
    """Guards the guard. Every assertion in this file is of the form "this
    route carries X", and a walker that returned nothing would make all of
    them pass while proving the opposite."""

    def test_the_walker_finds_every_auth_route(self, app: FastAPI) -> None:
        paths = {path for path, _ in api_routes(app)}

        assert paths >= set(LIMITED_PATHS)
        assert "/api/v1/auth/me" in paths

    def test_every_auth_endpoint_has_had_a_limiting_decision(self, app: FastAPI) -> None:
        """Every auth route is either limited or listed here as deliberately
        not — the check that notices an endpoint added with neither.

        A **set** rather than a count, and A64-020.6 is why. This asserted
        `len(...) == 11` and A64-020.2 added five `/auth/browser/*` routes
        without updating it, so it went red and stayed red for four phases
        — which is worse than useless: a permanently failing check is one
        nobody reads, and this one had genuinely noticed something.

        A count says "a number changed". A set says *which* endpoint nobody
        decided about, which is the sentence somebody can act on.

        The unguarded four, each with its reason:

            /auth/me           a read of the caller's own claims. Bounded by
                               the platform-wide limit; a per-endpoint rule
                               would throttle ordinary page loads
            /auth/logout       ending a session must not be refusable. A
            /auth/logout-all   locked-out user signing out everywhere is
                               exactly who needs it to work
            /auth/email/verify a one-time token, single-use by construction,
                               and the sender is already limited
            /auth/ws-ticket    one ticket per socket, and a client reconnects
                               on a flaky network — a rule tuned for sign-in
                               attempts would refuse ordinary reconnection
        """
        deliberately_unlimited = {
            "/api/v1/auth/me",
            "/api/v1/auth/logout",
            "/api/v1/auth/logout-all",
            "/api/v1/auth/browser/logout",
            "/api/v1/auth/browser/logout-all",
            "/api/v1/auth/email/verify",
            "/api/v1/auth/ws-ticket",
        }
        auth_paths = {path for path, _ in api_routes(app) if path.startswith("/api/v1/auth/")}

        undecided = auth_paths - set(LIMITED_PATHS) - deliberately_unlimited
        assert not undecided, f"these auth endpoints are neither limited nor listed: {undecided}"


class TestEveryListedEndpointIsGuarded:
    @pytest.mark.parametrize("path", LIMITED_PATHS)
    def test_the_route_carries_a_rate_limit_guard(self, app: FastAPI, path: str) -> None:
        assert guards_on(app, path), f"{path} has no RateLimit dependency"

    @pytest.mark.parametrize("path", LIMITED_PATHS)
    def test_the_route_carries_exactly_one_guard(self, app: FastAPI, path: str) -> None:
        """Two guards on one route would evaluate as two independent
        all-or-nothing groups, which reintroduces the double-charging the
        single-call contract exists to prevent."""
        assert len(guards_on(app, path)) == 1

    @pytest.mark.parametrize("path", LIMITED_PATHS)
    def test_the_route_documents_a_429(self, app: FastAPI, path: str) -> None:
        """A 429 a client has not been told to expect is one it retries
        immediately, in a loop."""
        responses = app.openapi()["paths"][path]["post"]["responses"]

        assert "429" in responses


class TestTheSpecifiedLimits:
    @pytest.mark.parametrize(
        ("path", "rule_name", "scope", "limit", "window_seconds"),
        [pytest.param(*row, id=f"{row[1]}") for row in EXPECTED],
    )
    def test_the_rule_matches_the_brief(
        self,
        app: FastAPI,
        path: str,
        rule_name: str,
        scope: RateLimitScope,
        limit: int,
        window_seconds: int,
    ) -> None:
        settings = RateLimitSettings()
        rules = {rule.name: rule for rule in guards_on(app, path)[0].rules(settings)}

        assert rule_name in rules, f"{path} does not carry rule {rule_name!r}"
        rule = rules[rule_name]
        assert rule.scope is scope
        assert rule.limit == limit
        assert rule.window == timedelta(seconds=window_seconds)


class TestLoginCarriesBothDimensions:
    """Per-IP and per-email answer different attacks and neither is
    sufficient — see `rate_limits.py`. Login is where both meet."""

    def test_login_has_two_rules(self, app: FastAPI) -> None:
        assert len(guards_on(app, "/api/v1/auth/login")[0].rules(RateLimitSettings())) == 2

    def test_login_counts_both_the_host_and_the_account(self, app: FastAPI) -> None:
        scopes = {
            rule.scope
            for rule in guards_on(app, "/api/v1/auth/login")[0].rules(RateLimitSettings())
        }

        assert scopes == {RateLimitScope.IP, RateLimitScope.EMAIL}


class TestRulesFollowConfiguration:
    def test_lowering_a_limit_lowers_the_rule(self) -> None:
        """The property an incident depends on: a limit is answered by an
        environment variable and a restart, not by a release."""
        rules = build_rules(RateLimitSettings(login_ip_limit=1))

        assert rules["login"][0].limit == 1

    def test_every_endpoint_key_resolves(self) -> None:
        """A guard naming an endpoint absent from `build_rules` would raise
        `KeyError` on its first request rather than at startup. Asserting
        the mapping here is what keeps that from being discovered in
        production."""
        rules = build_rules(RateLimitSettings())

        assert set(rules) == {
            "login",
            "register",
            "forgot_password",
            "reset_password",
            "resend_verification",
            "refresh",
        }

    def test_no_endpoint_resolves_to_an_empty_rule_set(self) -> None:
        """An empty tuple limits nothing while looking like a limit."""
        rules = build_rules(RateLimitSettings())

        assert all(rules[endpoint] for endpoint in rules)

    def test_rule_names_are_unique_across_endpoints(self) -> None:
        """Two endpoints sharing a rule name share a Redis bucket, so one
        endpoint's traffic would consume the other's allowance — a bug that
        presents as "sometimes login is limited and nobody knows why"."""
        names = [
            rule.name for endpoint in build_rules(RateLimitSettings()).values() for rule in endpoint
        ]

        assert len(names) == len(set(names))


class TestUnguardedEndpoints:
    """The four endpoints A64-011.8 does not list.

    Asserted explicitly rather than left unstated, so that adding a guard
    to one of them is a deliberate change to this list rather than a silent
    change in behaviour — and so a reader can see the omission was noticed.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/auth/logout",
            "/api/v1/auth/logout-all",
            "/api/v1/auth/email/verify",
        ],
    )
    def test_is_not_rate_limited(self, app: FastAPI, path: str) -> None:
        assert guards_on(app, path) == []

    def test_me_is_not_rate_limited(self, app: FastAPI) -> None:
        found = [
            dependency.call
            for route in app.routes
            if isinstance(route, APIRoute) and route.path == "/api/v1/auth/me"
            for dependency in route.dependant.dependencies
            if isinstance(dependency.call, RateLimit)
        ]

        assert found == []
