"""Which endpoints may answer with an email address — A64-031.E.

An address is personal data and, on this platform, is shown only to the
account that owns it (and to administrators). Nothing public — profiles,
search, leaderboards, games, tournaments, friends — carries one, and the
settings screen that shows it reads the self-only `GET /auth/me`.

This test pins that boundary against the OpenAPI document rather than
against any one serializer: it walks every success response, follows every
`$ref`, and lists the operations that can reach a schema with an `email`
property. A new endpoint that exposes an address — or a public schema that
gains one — changes that list and fails here, so the decision is made on
purpose and in review rather than discovered later.
"""

from typing import Any

import pytest

from app.app_factory import create_app

#: Every operation allowed to return an address, and why.
ALLOWED: dict[tuple[str, str], str] = {
    # The caller's own account — the token's `sub`; no identifier is taken.
    ("GET", "/api/v1/auth/me"): "self",
    ("POST", "/api/v1/auth/register"): "self, just created",
    ("POST", "/api/v1/auth/email/verify"): "self, just verified",
    ("POST", "/api/v1/auth/email/verify-code"): "self, just verified",
    ("POST", "/api/v1/auth/browser/register"): "self, just created",
    ("POST", "/api/v1/auth/browser/login"): "self, just signed in",
    ("POST", "/api/v1/auth/browser/refresh"): "self, the cookie's session",
    ("POST", "/api/v1/auth/browser/google/account"): "self, just created",
    ("POST", "/api/v1/auth/browser/google/link"): "self, just linked",
    # The address Google vouched for, to the browser holding that sign-in.
    ("GET", "/api/v1/auth/browser/google/pending"): "the pending sign-in's own",
    # Behind the administrator check.
    ("GET", "/api/v1/admin/users"): "administrators only",
    ("GET", "/api/v1/admin/users/{user_id}"): "administrators only",
}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return create_app().openapi()


def _refs(node: object, found: set[str]) -> set[str]:
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str):
            found.add(reference.rsplit("/", 1)[1])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)
    return found


def _reachable(names: set[str], schemas: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in seen or name not in schemas:
            continue
        seen.add(name)
        pending.extend(_refs(schemas[name], set()))
    return seen


def _exposing(spec: dict[str, Any]) -> set[tuple[str, str]]:
    schemas: dict[str, Any] = spec["components"]["schemas"]
    with_email = {
        name for name, schema in schemas.items() if "email" in schema.get("properties", {})
    }
    exposing: set[tuple[str, str]] = set()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            for status, response in operation.get("responses", {}).items():
                if not status.startswith("2"):
                    continue
                if _reachable(_refs(response, set()), schemas) & with_email:
                    exposing.add((method.upper(), path))
    return exposing


def test_only_self_and_admin_endpoints_answer_with_an_address(spec: dict[str, Any]) -> None:
    assert _exposing(spec) == set(ALLOWED)


def test_the_walk_finds_something(spec: dict[str, Any]) -> None:
    """Guards the guard: a walk that found nothing would pass the test
    above only if `ALLOWED` were empty too — but a broken `$ref` follower
    should fail loudly, not quietly agree."""
    assert ("GET", "/api/v1/auth/me") in _exposing(spec)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/profiles/{username}",
        "/api/v1/users/search",
        "/api/v1/leaderboard",
        "/api/v1/friends",
        "/api/v1/players/{player_id}/matches",
        "/api/v1/matches/{match_id}/replay",
    ],
)
def test_public_surfaces_carry_no_address(spec: dict[str, Any], path: str) -> None:
    """Named explicitly so a reader sees the boundary, not only a set. The
    path must exist — a renamed route would otherwise pass here by
    checking nothing."""
    assert path in spec["paths"]
    assert not any(exposed_path == path for _, exposed_path in _exposing(spec))
