"""Module-wide guarantees the `auth` API makes, asserted once over every
endpoint and every schema rather than per endpoint.

Added by A64-011.9's audit, which is the only reason a new test file is
justified in a task whose brief says not to write exhaustive tests. Each
assertion below covers a property that (a) every endpoint is supposed to
have, (b) nothing was checking, and (c) is invisible when broken — a new
endpoint added without it looks exactly like one with it.

The three are:

  **`extra="forbid"` on every request schema.** The protection against a
  client smuggling a field the handler was never meant to accept —
  `password_hash` into registration, `user_id` into a password reset. It
  is inherited from `BaseRequestDTO` and *also* re-declared on most
  schemas, so it currently holds for two independent reasons; this asserts
  the property rather than either mechanism, which is what keeps it true
  if one of them is removed.

  **`ErrorResponse` on every declared error status.** The platform returns
  exactly one error shape, and `apps/web`'s parser depends on it. A
  response declared without a model renders in the docs as an untyped
  `{}`, and a generated client gets no type for the branch it most needs
  one for.

  **No credential field escapes in a response schema.** Asserted against
  the generated OpenAPI rather than by reading the code, because the thing
  that would leak is a *serialisation*, and the schema is what says what
  gets serialised.

None of these replaces the per-endpoint suites. They exist so that the
eleventh endpoint cannot quietly be the first one to break the pattern.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.app_factory import create_app
from app.core.dto import BaseRequestDTO
from app.modules.auth.presentation import schemas as auth_schemas

#: Every request schema the module exposes, discovered rather than listed
#: — a hand-written list is one a new schema is simply left off.
REQUEST_SCHEMAS = sorted(
    (
        getattr(auth_schemas, name)
        for name in auth_schemas.__all__
        if isinstance(getattr(auth_schemas, name), type)
        and issubclass(getattr(auth_schemas, name), BaseRequestDTO)
    ),
    key=lambda schema: schema.__name__,
)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return create_app().openapi()


def auth_operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, operations in sorted(spec["paths"].items())
        if "/auth/" in path
        for method, operation in operations.items()
    ]


def test_the_module_exposes_request_schemas_to_check() -> None:
    """Guards the guard: a discovery helper that found nothing would make
    every parametrised test below vacuously pass."""
    assert len(REQUEST_SCHEMAS) >= 6


class TestRequestSchemasRejectUnknownFields:
    @pytest.mark.parametrize("schema", REQUEST_SCHEMAS, ids=lambda s: s.__name__)
    def test_extra_fields_are_forbidden(self, schema: type[BaseRequestDTO]) -> None:
        assert schema.model_config.get("extra") == "forbid"

    @pytest.mark.parametrize("schema", REQUEST_SCHEMAS, ids=lambda s: s.__name__)
    def test_a_smuggled_field_is_rejected_rather_than_ignored(
        self, schema: type[BaseRequestDTO]
    ) -> None:
        """The behavioural half. `password_hash` is the field that matters:
        a client that could set it on registration would choose its own
        stored credential."""
        with pytest.raises(ValidationError):
            schema.model_validate({"password_hash": "argon2id$attacker$chosen"})


class TestEveryErrorResponseIsTyped:
    def test_all_declared_error_statuses_carry_the_platform_error_model(
        self, spec: dict[str, Any]
    ) -> None:
        untyped: list[str] = []
        for path, method, operation in auth_operations(spec):
            for status, response in operation.get("responses", {}).items():
                if not status.startswith(("4", "5")):
                    continue
                schema = response.get("content", {}).get("application/json", {}).get("schema", {})
                if "ErrorResponse" not in str(schema):
                    untyped.append(f"{method.upper()} {path} -> {status}")

        assert untyped == []

    def test_every_endpoint_declares_at_least_one_error_status(self, spec: dict[str, Any]) -> None:
        """An endpoint declaring only its success is an endpoint whose
        client has no branch for failure. Every route on this module can
        fail — at minimum with a 401 or a 422."""
        for path, method, operation in auth_operations(spec):
            statuses = [s for s in operation.get("responses", {}) if s.startswith(("4", "5"))]
            assert statuses, f"{method.upper()} {path} declares no error status"


class TestNoCredentialLeaksIntoAResponse:
    #: Field names that must never appear in a **response** schema. The
    #: token pair is the deliberate exception and is excluded by name at
    #: the assertion, not here — a client cannot use a token it is not
    #: given.
    FORBIDDEN = ("password", "password_hash", "token_hash", "refresh_token_hash", "secret")

    def test_no_response_schema_exposes_a_credential_field(self, spec: dict[str, Any]) -> None:
        offenders: list[str] = []
        for path, method, operation in auth_operations(spec):
            for status, response in operation.get("responses", {}).items():
                if not status.startswith("2"):
                    continue
                schema = str(response.get("content", {}).get("application/json", {}))
                for field in self.FORBIDDEN:
                    # `refresh_token` is the one credential these endpoints
                    # legitimately return; `password`/`*_hash` never are.
                    if f'"{field}"' in schema:
                        offenders.append(f"{method.upper()} {path} -> {status}: {field}")

        assert offenders == []

    def test_the_account_view_carries_no_hash(self, spec: dict[str, Any]) -> None:
        """`UserRead` is what `POST /auth/register` and `GET /auth/me`
        return. The absence of `password_hash` is a property of the type
        rather than of remembering to strip it — this is the assertion that
        keeps it one."""
        user_read = spec["components"]["schemas"]["UserRead"]

        assert "password_hash" not in user_read["properties"]
        assert "password" not in user_read["properties"]


class TestDocumentationCompleteness:
    @pytest.mark.parametrize("field", ["summary", "description"])
    def test_every_endpoint_is_documented(self, spec: dict[str, Any], field: str) -> None:
        for path, method, operation in auth_operations(spec):
            assert (operation.get(field) or "").strip(), f"{method.upper()} {path} has no {field}"

    def test_every_endpoint_is_tagged_and_the_tag_is_described(self, spec: dict[str, Any]) -> None:
        described = {tag["name"] for tag in spec.get("tags", []) if tag.get("description")}

        for path, method, operation in auth_operations(spec):
            tags = operation.get("tags", [])
            assert tags, f"{method.upper()} {path} is untagged"
            assert set(tags) <= described, f"{method.upper()} {path} uses an undescribed tag"

    def test_every_request_body_carries_an_example(self, spec: dict[str, Any]) -> None:
        """A64-011.9 found `LoginRequest` and `RegisterRequest` without
        one — the two endpoints most likely to be the first thing anyone
        tries from the docs page."""
        for path, method, operation in auth_operations(spec):
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
            reference = body.get("schema", {}).get("$ref")
            if not reference:
                continue

            name = reference.rsplit("/", 1)[-1]
            schema = spec["components"]["schemas"][name]
            assert schema.get("examples") or schema.get("example"), (
                f"{method.upper()} {path} body schema {name} has no example"
            )


class TestAppWiring:
    def test_the_removed_state_reader_is_gone(self) -> None:
        """A64-011.9 removed `require_authentication` — dead, and one shift
        key from the live `RequireAuthentication` guard. Asserted so that
        re-adding it is a deliberate act rather than a merge artefact."""
        from app.modules.auth.presentation import dependencies

        assert not hasattr(dependencies, "require_authentication")
        assert hasattr(dependencies, "RequireAuthentication")

    def test_the_clock_has_one_source(self) -> None:
        """A64-011.9 moved `get_clock` to `app.api.deps`; `users` re-exports
        it. Two independent `SystemClock` factories would mean a test that
        froze time in one place and not the other."""
        from app.api.deps import get_clock as platform_clock
        from app.modules.users.presentation.dependencies import get_clock as users_clock

        assert users_clock is platform_clock

    def test_the_app_builds(self, spec: dict[str, Any]) -> None:
        """The app assembles and publishes exactly the auth surface it
        should — no endpoint added without anybody noticing, and none
        silently dropped.

        A **set**, not a count. This asserted `== 11` and A64-020.2 added
        five `/auth/browser/*` routes without updating it, so it failed for
        four phases and was read by nobody: a check that is always red has
        stopped communicating, which is worse than not existing.

        The same replacement `test_auth_rate_limits.py` received, for the
        same reason. A count says a number changed; a set names the
        endpoint, which is the sentence somebody can act on.
        """
        assert isinstance(create_app(), FastAPI)

        published = {path for path in spec["paths"] if "/auth/" in path}
        assert published == {
            # Bearer tokens — the original surface, A64-011.
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/logout",
            "/api/v1/auth/logout-all",
            "/api/v1/auth/me",
            "/api/v1/auth/password/forgot",
            "/api/v1/auth/password/reset",
            "/api/v1/auth/email/verify",
            "/api/v1/auth/email/resend",
            # The code flow that replaced the link at registration —
            # A64-021.5H. Added here in A64-024's closing sweep, having
            # shipped without it: the set was red from that phase on, which
            # is the exact failure this docstring already describes.
            "/api/v1/auth/email/verify-code",
            "/api/v1/auth/email/resend-code",
            # AD-09's credential, minted over HTTP because a browser cannot
            # set headers on a WebSocket handshake — A64-016.1.
            "/api/v1/auth/ws-ticket",
            # The cookie surface the browser client uses — A64-020.2.
            "/api/v1/auth/browser/register",
            "/api/v1/auth/browser/login",
            "/api/v1/auth/browser/refresh",
            "/api/v1/auth/browser/logout",
            "/api/v1/auth/browser/logout-all",
        }
