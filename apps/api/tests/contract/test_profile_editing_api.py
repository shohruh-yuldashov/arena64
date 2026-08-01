"""`PATCH /profile` and `GET /profile/me` end to end — real PostgreSQL,
the real composition root.

A64-012.3 asks for essential tests only and names four: successful update,
invalid country, invalid language, bio length validation. All four are
here.

What else is here is the small set of properties that would be *silently*
wrong rather than loudly broken:

  - **PATCH semantics** — omitted leaves alone, explicit `null` clears.
    A shape that collapsed the two would pass a "successful update" test
    and quietly wipe fields nobody mentioned;
  - **mass assignment** — a body carrying `username` or `is_verified` is
    rejected, not applied and not silently dropped;
  - **normalisation round-trips** — a padded name comes back trimmed and
    `uz` comes back `UZ`, so a client renders what was stored;
  - **partial failure writes nothing** — a request with a good bio and a
    bad country changes neither.

This file also inherits the coverage of the retired `PATCH /users/{id}`
tests; see `tests/unit/test_users_api.py` for what moved and why.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_rate_limiter
from app.app_factory import create_app
from app.modules.users.domain.validators import BIO_MAX_LENGTH, DISPLAY_NAME_MAX_LENGTH
from tests.fakes.rate_limiter import AllowAllRateLimiter

PROFILE_URL = "/api/v1/profile"
ME_URL = "/api/v1/profile/me"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app with only the session and the rate limiter
    redirected. No `dependency_overrides` on any profile service — the
    graph under test is the one that ships."""
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield contract_session

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_rate_limiter] = lambda: AllowAllRateLimiter()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth(client: AsyncClient) -> dict[str, str]:
    suffix = uuid4().hex[:10]
    account = {
        "username": f"player{suffix}",
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }
    registered = await client.post(REGISTER_URL, json=account)
    assert registered.status_code == 201, registered.text

    signed_in = await client.post(LOGIN_URL, json={"email": account["email"], "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    return {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"}


async def patch(client: AsyncClient, auth: dict[str, str], body: dict[str, Any]) -> Any:
    return await client.patch(PROFILE_URL, headers=auth, json=body)


class TestSuccessfulUpdate:
    async def test_updates_every_editable_field(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await patch(
            client,
            auth,
            {
                "display_name": "Жанибек Алиев",
                "bio": "Blitz player.",
                "country": "UZ",
            },
        )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["display_name"] == "Жанибек Алиев"
        assert data["bio"] == "Blitz player."
        assert data["country"] == "UZ"

    async def test_the_change_persists(self, client: AsyncClient, auth: dict[str, str]) -> None:
        await patch(client, auth, {"bio": "Persisted."})

        assert (await client.get(ME_URL, headers=auth)).json()["data"]["bio"] == "Persisted."

    async def test_values_come_back_normalised(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """A client that echoed its own request would render something the
        platform did not store."""
        data = (await patch(client, auth, {"display_name": "  Padded  ", "country": "uz"})).json()[
            "data"
        ]

        assert data["display_name"] == "Padded"
        assert data["country"] == "UZ"

    async def test_an_omitted_field_is_left_alone(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        await patch(client, auth, {"display_name": "Keep Me"})

        await patch(client, auth, {"country": "GB"})

        data = (await client.get(ME_URL, headers=auth)).json()["data"]
        assert data["display_name"] == "Keep Me"
        assert data["country"] == "GB"

    async def test_an_explicit_null_clears_a_nullable_field(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """The half a `None`-means-absent shape could not express."""
        await patch(client, auth, {"bio": "Temporary."})

        assert (await patch(client, auth, {"bio": None})).json()["data"]["bio"] is None

    async def test_an_empty_body_is_accepted(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        assert (await patch(client, auth, {})).status_code == 200

    async def test_unicode_display_names_are_accepted(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """The platform serves Uzbek and Russian speakers; an ASCII-only
        rule would tell a large share of them their own name is invalid."""
        for name in ("Жанибек", "李小龍", "Ünsal Öz"):
            assert (await patch(client, auth, {"display_name": name})).status_code == 200

    async def test_the_public_profile_reflects_the_change(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        await patch(client, auth, {"bio": "Visible to all.", "country": "GB"})
        username = (await client.get("/api/v1/auth/me", headers=auth)).json()["data"]["username"]

        public = await client.get(f"/api/v1/profiles/{username}")

        assert public.json()["data"]["bio"] == "Visible to all."
        assert public.json()["data"]["country"] == "GB"


class TestInvalidCountry:
    @pytest.mark.parametrize(
        "country",
        ["XX", "ZZ", "QQ", "GBR", "1A", "u", "United Kingdom"],
        ids=["private-XX", "private-ZZ", "unassigned-QQ", "alpha3", "digit", "too-short", "name"],
    )
    async def test_an_unassigned_or_malformed_code_is_rejected(
        self, client: AsyncClient, auth: dict[str, str], country: str
    ) -> None:
        """`XX` and `ZZ` are *well-formed* and belong to ISO's private-use
        range — accepting them would put arbitrary two-letter strings in
        the column by the front door."""
        response = await patch(client, auth, {"country": country})

        assert response.status_code == 422, f"{country!r} was accepted"

    async def test_a_rejected_country_leaves_the_profile_unchanged(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        await patch(client, auth, {"country": "GB"})

        await patch(client, auth, {"country": "XX"})

        assert (await client.get(ME_URL, headers=auth)).json()["data"]["country"] == "GB"


class TestLocaleMovedToPreferences:
    """A64-012.5 moved `preferred_language` and `timezone` off this
    endpoint, so that a language has one writable path rather than two.

    The positive coverage — every supported language accepted, an
    unsupported one rejected, a bad timezone rejected — moved with them and
    lives in `tests/contract/test_preferences_api.py`. What stays here is
    the assertion that this endpoint no longer accepts either, because a
    field that quietly returned to a second writable surface is precisely
    the regression the move exists to prevent.
    """

    @pytest.mark.parametrize("field", ["preferred_language", "timezone"])
    async def test_the_locale_fields_are_rejected_here(
        self, client: AsyncClient, auth: dict[str, str], field: str
    ) -> None:
        value = "uz" if field == "preferred_language" else "Asia/Tashkent"

        response = await patch(client, auth, {field: value})

        # Rejected, not ignored. A silently dropped timezone would look
        # like a successful change to a client that had not noticed the
        # move.
        assert response.status_code == 422
        assert field in response.text

    async def test_the_preferences_endpoint_accepts_them(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """The other half of the move: rejected here, accepted there. A
        test that only asserted the rejection would pass just as well if
        the fields had been dropped from the platform entirely."""
        response = await client.patch(
            "/api/v1/profile/preferences",
            headers=auth,
            json={"locale": {"preferred_language": "uz", "timezone": "Asia/Tashkent"}},
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["locale"] == {
            "preferred_language": "uz",
            "timezone": "Asia/Tashkent",
        }


class TestBioLengthValidation:
    async def test_a_bio_at_the_limit_is_accepted(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await patch(client, auth, {"bio": "x" * BIO_MAX_LENGTH})

        assert response.status_code == 200
        assert len(response.json()["data"]["bio"]) == BIO_MAX_LENGTH

    async def test_a_bio_over_the_limit_is_rejected(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        assert (await patch(client, auth, {"bio": "x" * (BIO_MAX_LENGTH + 1)})).status_code == 422

    @pytest.mark.parametrize(
        ("label", "bio"),
        [
            pytest.param("ansi escape", "bad\x1b[31mred", id="control-char"),
            pytest.param("rtl override", "safe‮txet lamron", id="bidi-override"),
        ],
    )
    async def test_control_and_bidi_characters_are_rejected(
        self, client: AsyncClient, auth: dict[str, str], label: str, bio: str
    ) -> None:
        """Reused from A64-012.1's `validate_bio` — the requirement says
        existing validation must be reused, and this is the assertion that
        it actually is rather than reimplemented."""
        assert (await patch(client, auth, {"bio": bio})).status_code == 422

    async def test_the_error_never_echoes_the_rejected_bio(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        response = await patch(client, auth, {"bio": "SECRET" + "x" * BIO_MAX_LENGTH})

        assert "SECRET" not in response.text


class TestDisplayNameValidation:
    @pytest.mark.parametrize("name", ["ab", " a ", "x" * (DISPLAY_NAME_MAX_LENGTH + 1)])
    async def test_out_of_range_names_are_rejected(
        self, client: AsyncClient, auth: dict[str, str], name: str
    ) -> None:
        assert (await patch(client, auth, {"display_name": name})).status_code == 422

    async def test_control_characters_are_rejected(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """A display name is rendered beside every match, which makes it a
        better place to hide an override than a bio nobody scrolls to."""
        assert (await patch(client, auth, {"display_name": "Ann‮e"})).status_code == 422

    async def test_display_name_can_be_cleared(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        await patch(client, auth, {"display_name": "Removable"})

        assert (await patch(client, auth, {"display_name": None})).json()["data"][
            "display_name"
        ] is None


class TestMassAssignment:
    @pytest.mark.parametrize(
        "field",
        ["username", "email", "is_verified", "is_active", "id", "avatar_object_key", "password"],
    )
    async def test_a_field_this_endpoint_does_not_own_is_rejected(
        self, client: AsyncClient, auth: dict[str, str], field: str
    ) -> None:
        """Rejected, not ignored. A64-012.3 says "ignore unknown fields";
        this refuses them, which satisfies "prevent mass assignment" more
        strongly — a silently dropped `username` looks to the client like a
        successful rename."""
        response = await patch(client, auth, {field: "anything"})

        assert response.status_code == 422, f"{field!r} was accepted"

    async def test_a_rejected_body_changes_nothing(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        before = (await client.get(ME_URL, headers=auth)).json()["data"]

        await patch(client, auth, {"display_name": "Valid Name", "username": "hijacked"})

        assert (await client.get(ME_URL, headers=auth)).json()["data"] == before

    async def test_the_username_is_unchanged_by_a_valid_update(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """A64-012.3 excludes username changes; this is the assertion that
        editing a profile cannot become a rename by accident."""
        before = (await client.get(ME_URL, headers=auth)).json()["data"]["username"]

        after = (await patch(client, auth, {"display_name": "New Name"})).json()["data"]

        assert after["username"] == before

    async def test_a_partly_invalid_request_writes_nothing(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """Values are validated before the row is touched, so a good bio
        beside a bad country persists neither."""
        await patch(client, auth, {"bio": "Original."})

        await patch(client, auth, {"bio": "Replacement.", "country": "XX"})

        assert (await client.get(ME_URL, headers=auth)).json()["data"]["bio"] == "Original."


class TestOnlyYourOwnProfile:
    @pytest.mark.parametrize("method", ["get", "patch"])
    async def test_authentication_is_required(self, client: AsyncClient, method: str) -> None:
        """There is no path segment or body field naming an account, so
        "may I edit this one" is not a question these endpoints can be
        asked — authentication is the whole of the authorization."""
        if method == "get":
            response = await client.get(ME_URL)
        else:
            response = await client.patch(PROFILE_URL, json={"bio": "x"})

        assert response.status_code == 401

    async def test_one_account_cannot_edit_another(self, client: AsyncClient) -> None:
        """Two real accounts; each edit lands on the token's own profile."""
        suffixes = [uuid4().hex[:10] for _ in range(2)]
        tokens = []
        for suffix in suffixes:
            account = {
                "username": f"player{suffix}",
                "email": f"{suffix}@example.com",
                "password": PASSWORD,
            }
            await client.post(REGISTER_URL, json=account)
            signed_in = await client.post(
                LOGIN_URL, json={"email": account["email"], "password": PASSWORD}
            )
            tokens.append({"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"})

        await patch(client, tokens[0], {"bio": "First player."})
        await patch(client, tokens[1], {"bio": "Second player."})

        assert (await client.get(ME_URL, headers=tokens[0])).json()["data"]["bio"] == (
            "First player."
        )
        assert (await client.get(ME_URL, headers=tokens[1])).json()["data"]["bio"] == (
            "Second player."
        )

    async def test_the_retired_unauthenticated_endpoint_is_gone(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """A64-012.3 removed `PATCH /users/{user_id}`, which let anyone
        holding a public player id rewrite that player's profile. Asserted
        so re-adding it is a deliberate act rather than a merge artefact."""
        user_id = (await client.get("/api/v1/auth/me", headers=auth)).json()["data"]["id"]

        response = await client.patch(f"/api/v1/users/{user_id}", json={"display_name": "Hijack"})

        assert response.status_code == 405


class TestReadOwnProfile:
    async def test_returns_the_owner_view(self, client: AsyncClient, auth: dict[str, str]) -> None:
        data = (await client.get(ME_URL, headers=auth)).json()["data"]

        # No `language` and no `timezone` since A64-012.5: this response
        # reports the fields `PATCH /profile` can change, and it can no
        # longer change those two. `GET /profile/preferences` has them.
        assert set(data) == {
            "id",
            "username",
            "display_name",
            "bio",
            "country",
            "avatar_url",
            "thumbnail_url",
            "joined_at",
        }

    async def test_no_view_of_a_profile_publishes_a_timezone(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """Publishing a timezone narrows a player's physical location to
        anyone who asks, so no profile endpoint carries one — and since
        A64-012.5 not even the owner's, which reads it from
        `GET /profile/preferences` instead."""
        await client.patch(
            "/api/v1/profile/preferences",
            headers=auth,
            json={"locale": {"timezone": "Asia/Tashkent"}},
        )
        username = (await client.get("/api/v1/auth/me", headers=auth)).json()["data"]["username"]

        mine = (await client.get(ME_URL, headers=auth)).json()["data"]
        public = (await client.get(f"/api/v1/profiles/{username}")).json()["data"]
        preferences = (await client.get("/api/v1/profile/preferences", headers=auth)).json()["data"]

        assert "timezone" not in mine
        assert "timezone" not in public
        assert preferences["locale"]["timezone"] == "Asia/Tashkent"

    async def test_carries_no_email_or_account_state(
        self, client: AsyncClient, auth: dict[str, str]
    ) -> None:
        """Credentials and verification state are `auth`'s and are served
        by `GET /auth/me`; a second copy here would be a second thing to
        keep in step."""
        response = await client.get(ME_URL, headers=auth)

        for forbidden in ("email", "password", "is_verified", "is_active", "locked_until"):
            assert forbidden not in response.text


class TestOpenApi:
    async def test_both_endpoints_are_documented(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()

        for path, method in ((PROFILE_URL, "patch"), (ME_URL, "get")):
            operation = spec["paths"][path][method]
            assert operation["summary"]
            assert operation["description"].strip()
            assert operation["tags"] == ["profile"]
            assert "422" in operation["responses"] or method == "get"

    async def test_the_request_schema_documents_every_editable_field(
        self, client: AsyncClient
    ) -> None:
        spec = (await client.get("/openapi.json")).json()
        schema = spec["components"]["schemas"]["ProfileUpdateRequest"]

        assert set(schema["properties"]) == {"display_name", "bio", "country"}
        assert schema.get("additionalProperties") is False
        assert schema.get("examples")

    async def test_error_responses_carry_the_platform_model(self, client: AsyncClient) -> None:
        spec = (await client.get("/openapi.json")).json()
        operation = spec["paths"][PROFILE_URL]["patch"]

        for status in ("401", "404", "422"):
            rendered = str(operation["responses"][status])
            assert "ErrorResponse" in rendered
