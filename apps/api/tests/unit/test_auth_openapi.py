"""The generated OpenAPI document for the authentication API.

Documentation is a deliverable here, not a side effect, so it is asserted
like one. These tests need no database and no fixtures: the schema is a
pure function of the route declarations, which means a missing summary or
an undeclared 401 fails in milliseconds rather than being noticed by
whoever reads the docs months later.

What is *not* asserted: prose quality. These check the structural
properties a client generator and a reader depend on — every operation is
described, every documented failure has a shape, and no schema leaks a
credential field.
"""

from typing import Any

import pytest

from app.app_factory import create_app

AUTH_PATHS = {
    "/api/v1/auth/register": "post",
    "/api/v1/auth/login": "post",
    "/api/v1/auth/refresh": "post",
    "/api/v1/auth/logout": "post",
    "/api/v1/auth/logout-all": "post",
    "/api/v1/auth/me": "get",
    "/api/v1/auth/email/verify": "post",
    "/api/v1/auth/email/resend": "post",
}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return create_app().openapi()


def operation(schema: dict[str, Any], path: str) -> dict[str, Any]:
    return schema["paths"][path][AUTH_PATHS[path]]  # type: ignore[no-any-return]


class TestEveryEndpointExists:
    @pytest.mark.parametrize("path", sorted(AUTH_PATHS))
    def test_the_operation_is_published(self, schema: dict[str, Any], path: str) -> None:
        assert path in schema["paths"]
        assert AUTH_PATHS[path] in schema["paths"][path]

    def test_all_of_them_are_tagged_auth(self, schema: dict[str, Any]) -> None:
        """So the rendered docs group them, rather than scattering six
        endpoints through an alphabetical list."""
        for path in AUTH_PATHS:
            assert operation(schema, path)["tags"] == ["auth"]


class TestDocumentation:
    @pytest.mark.parametrize("path", sorted(AUTH_PATHS))
    def test_has_a_summary(self, schema: dict[str, Any], path: str) -> None:
        summary = operation(schema, path).get("summary", "")

        assert summary
        # FastAPI derives a summary from the function name when none is
        # given ("Logout All"), which reads as a placeholder. A real one
        # says what the call does.
        assert summary != path.rsplit("/", maxsplit=1)[-1].replace("-", " ").title()

    @pytest.mark.parametrize("path", sorted(AUTH_PATHS))
    def test_has_a_description(self, schema: dict[str, Any], path: str) -> None:
        """FastAPI takes the description from the handler's docstring, so
        this is really asserting that every handler is documented."""
        assert len(operation(schema, path).get("description", "")) > 100

    @pytest.mark.parametrize("path", sorted(AUTH_PATHS))
    def test_describes_its_success_response(self, schema: dict[str, Any], path: str) -> None:
        responses = operation(schema, path)["responses"]
        success = next(code for code in responses if code.startswith("2"))

        assert responses[success]["description"]


class TestStatusCodes:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/api/v1/auth/register", "201"),
            ("/api/v1/auth/login", "200"),
            ("/api/v1/auth/refresh", "200"),
            ("/api/v1/auth/logout", "204"),
            ("/api/v1/auth/logout-all", "204"),
            ("/api/v1/auth/me", "200"),
            ("/api/v1/auth/email/verify", "200"),
            # 202, not 200: the work is handed to a mail provider and
            # the outcome is not known when the call returns.
            ("/api/v1/auth/email/resend", "202"),
        ],
    )
    def test_declares_the_documented_success_code(
        self, schema: dict[str, Any], path: str, expected: str
    ) -> None:
        assert expected in operation(schema, path)["responses"]

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/auth/logout", "/api/v1/auth/logout-all"],
    )
    def test_a_204_carries_no_body(self, schema: dict[str, Any], path: str) -> None:
        """`204 No Content` with a body is a contradiction that some
        clients handle by hanging."""
        assert "content" not in operation(schema, path)["responses"]["204"]


class TestErrorResponses:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/logout",
            "/api/v1/auth/logout-all",
            "/api/v1/auth/me",
        ],
    )
    def test_every_authenticated_endpoint_documents_401(
        self, schema: dict[str, Any], path: str
    ) -> None:
        assert "401" in operation(schema, path)["responses"]

    def test_register_documents_409(self, schema: dict[str, Any]) -> None:
        assert "409" in operation(schema, "/api/v1/auth/register")["responses"]

    def test_login_documents_403(self, schema: dict[str, Any]) -> None:
        """Deactivated and locked accounts — correct credentials, refused
        sign-in. A client shows different advice for 403 than for 401."""
        assert "403" in operation(schema, "/api/v1/auth/login")["responses"]

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/email/verify",
            "/api/v1/auth/email/resend",
        ],
    )
    def test_endpoints_with_bodies_document_422(self, schema: dict[str, Any], path: str) -> None:
        assert "422" in operation(schema, path)["responses"]

    @pytest.mark.parametrize("path", sorted(AUTH_PATHS))
    def test_documented_errors_use_the_platform_envelope(
        self, schema: dict[str, Any], path: str
    ) -> None:
        """Not FastAPI's default `{"detail": ...}`, which this platform
        never returns — a generated client built against that shape would
        fail to parse every real error."""
        responses = operation(schema, path)["responses"]

        for code, response in responses.items():
            if not code.startswith(("4", "5")) or "content" not in response:
                continue
            ref = response["content"]["application/json"]["schema"].get("$ref", "")
            assert ref.endswith("/ErrorResponse"), f"{path} {code} -> {ref}"


class TestSchemas:
    def test_login_and_refresh_share_one_response_schema(self, schema: dict[str, Any]) -> None:
        """ "Avoid duplicated response objects": both return `TokenPair`, so
        a client's token handling is written once."""
        login = operation(schema, "/api/v1/auth/login")["responses"]["200"]
        refresh = operation(schema, "/api/v1/auth/refresh")["responses"]["200"]

        assert (
            login["content"]["application/json"]["schema"]
            == refresh["content"]["application/json"]["schema"]
        )

    def test_token_pair_documents_every_field(self, schema: dict[str, Any]) -> None:
        properties = schema["components"]["schemas"]["TokenPair"]["properties"]

        assert set(properties) == {
            "access_token",
            "refresh_token",
            "token_type",
            "expires_in",
        }
        for field in properties.values():
            assert field.get("description")

    def test_token_pair_carries_an_example(self, schema: dict[str, Any]) -> None:
        assert schema["components"]["schemas"]["TokenPair"].get("examples")

    def test_refresh_request_carries_an_example(self, schema: dict[str, Any]) -> None:
        assert schema["components"]["schemas"]["RefreshRequest"].get("examples")

    def test_refresh_request_forbids_unknown_fields(self, schema: dict[str, Any]) -> None:
        """`extra="forbid"` survived the `model_config` override that added
        the example — a subclass config that dropped it would silently
        start accepting `user_id` alongside the token."""
        assert schema["components"]["schemas"]["RefreshRequest"]["additionalProperties"] is False

    def test_verification_schemas_carry_examples(self, schema: dict[str, Any]) -> None:
        for name in ("VerifyEmailRequest", "ResendVerificationRequest"):
            assert schema["components"]["schemas"][name].get("examples"), name

    def test_no_schema_exposes_a_password_or_a_hash(self, schema: dict[str, Any]) -> None:
        """A response model is the one place a credential field reaches
        every client at once. Asserted across the whole document rather
        than per-endpoint, so a new schema cannot quietly add one."""
        for name, definition in schema["components"]["schemas"].items():
            fields = set(definition.get("properties", {}))
            assert "password_hash" not in fields, name
            assert "refresh_token_hash" not in fields, name
            assert "token_hash" not in fields, name


class TestSecurityScheme:
    def test_bearer_authentication_is_advertised(self, schema: dict[str, Any]) -> None:
        """What puts the padlock in the docs and makes "Authorize" work."""
        schemes = schema["components"]["securitySchemes"]

        assert any(
            scheme.get("type") == "http" and scheme.get("scheme") == "bearer"
            for scheme in schemes.values()
        )

    @pytest.mark.parametrize("path", ["/api/v1/auth/me", "/api/v1/auth/logout-all"])
    def test_token_protected_operations_declare_security(
        self, schema: dict[str, Any], path: str
    ) -> None:
        """So the docs' "Try it out" sends the token, and so a generated
        client knows to attach one."""
        assert operation(schema, path).get("security")

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            # A64-011.6: both verification endpoints are deliberately
            # unauthenticated. The person redeeming a link may never
            # have signed in, and the person who needs a *resend* is by
            # definition the one who never received the first link.
            "/api/v1/auth/email/verify",
            "/api/v1/auth/email/resend",
        ],
    )
    def test_unauthenticated_operations_declare_none(
        self, schema: dict[str, Any], path: str
    ) -> None:
        """These three are how a caller *obtains* a token. Requiring one
        would be a documentation bug that makes the API look unusable."""
        assert not operation(schema, path).get("security")


class TestTheDocumentIsWellFormed:
    def test_it_parses_as_an_openapi_3_document(self, schema: dict[str, Any]) -> None:
        assert schema["openapi"].startswith("3.")
        assert schema["info"]["title"]

    def test_every_schema_reference_resolves(self, schema: dict[str, Any]) -> None:
        """A dangling `$ref` renders as a blank box in the docs and breaks
        every client generator."""
        defined = set(schema.get("components", {}).get("schemas", {}))
        referenced = _collect_refs(schema)

        assert referenced <= defined, f"dangling: {sorted(referenced - defined)}"


def _collect_refs(node: object) -> set[str]:
    if isinstance(node, dict):
        found: set[str] = set()
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value.rsplit("/", maxsplit=1)[-1])
            else:
                found |= _collect_refs(value)
        return found
    if isinstance(node, list):
        return set().union(*(_collect_refs(item) for item in node)) if node else set()
    return set()
