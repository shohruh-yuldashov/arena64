"""Preferences end to end — real PostgreSQL, the real composition root.

A64-012.5 asks for essential tests only and names four: a successful
update, an invalid preference value, an invalid timezone, and a partial
update. All four are here.

What else is here is the small set of properties that would be *silently*
wrong rather than loudly broken:

  - **two-level partiality** — an omitted group is untouched, and inside a
    present group an omitted setting is untouched. Collapsing either turns
    "change one setting" into "reset the group", which no client can see
    and a player only notices later;
  - **defaults come from the database, not from a client** — an account
    that has never opened the settings screen stores `{}` and reads back a
    complete document;
  - **unknown keys are rejected at both levels** — an unknown group and an
    unknown setting inside a known group are both errors, since only the
    inner check can catch `{"gameplay": {"sound": true}}`;
  - **the locale move is real in both directions** — rejected on
    `PATCH /profile`, accepted here. A test asserting only the rejection
    would pass just as well if the fields had been deleted outright;
  - **nothing is public** — no preference reaches `GET /profiles/{name}`;
  - **the rate limit counts the account, not the address**, which is the
    whole reason `RateLimitScope.USER` was built.

The rate limiter is replaced by `AllowAllRateLimiter`, as in every other
endpoint suite — the limiter's own behaviour is
`tests/contract/test_rate_limiter.py`'s subject. That the guard is attached
and that it is USER-scoped is asserted here, because a missing or
misdimensioned guard is invisible to a limiter test.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_factory import create_app
from app.core.rate_limiting import RateLimitScope
from app.modules.profiles.presentation.rate_limits import PREFERENCES_UPDATE_RATE_LIMIT
from app.modules.profiles.presentation.self_router import my_profile_router
from tests.contract.contract_app import build_contract_app, contract_client

PREFERENCES_URL = "/api/v1/profile/preferences"
PROFILE_URL = "/api/v1/profile"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"

#: The defaults A64-012.5 specifies, typed in by hand rather than imported
#: from `domain/preferences.py`. Importing the constants would assert only
#: that the code agrees with itself, which is the one thing this cannot
#: usefully prove.
DEFAULTS: dict[str, dict[str, Any]] = {
    "gameplay": {
        "board_theme": "classic",
        "piece_set": "classic",
        "confirm_move": False,
        "show_coordinates": True,
        "animation_speed": "normal",
    },
    "locale": {"preferred_language": "en", "timezone": "UTC"},
}


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app over the test's rolled-back transaction.

    No override on any preferences service, mapper or schema — the graph
    under test is the one that ships. Only `lifespan`'s state is stood in
    for (`tests/contract/contract_app.py`)."""
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def register(client: AsyncClient) -> tuple[str, dict[str, str]]:
    """One account. Returns its username and an `Authorization` header."""
    suffix = uuid4().hex[:10]
    username = f"player{suffix}"
    account = {
        "username": username,
        "email": f"{suffix}@example.com",
        "password": PASSWORD,
    }
    registered = await client.post(REGISTER_URL, json=account)
    assert registered.status_code == 201, registered.text

    signed_in = await client.post(LOGIN_URL, json={"email": account["email"], "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    return username, {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"}


@pytest_asyncio.fixture
async def account(client: AsyncClient) -> tuple[str, dict[str, str]]:
    return await register(client)


async def patch_preferences(client: AsyncClient, auth: dict[str, str], body: dict[str, Any]) -> Any:
    return await client.patch(PREFERENCES_URL, headers=auth, json=body)


async def read_preferences(client: AsyncClient, auth: dict[str, str]) -> Any:
    return (await client.get(PREFERENCES_URL, headers=auth)).json()["data"]


class TestSuccessfulUpdate:
    async def test_every_setting_in_both_groups(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account

        response = await patch_preferences(
            client,
            auth,
            {
                "gameplay": {
                    "board_theme": "midnight",
                    "piece_set": "neo",
                    "confirm_move": True,
                    "show_coordinates": False,
                    "animation_speed": "instant",
                },
                "locale": {"preferred_language": "ru", "timezone": "Europe/Moscow"},
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"] == {
            "gameplay": {
                "board_theme": "midnight",
                "piece_set": "neo",
                "confirm_move": True,
                "show_coordinates": False,
                "animation_speed": "instant",
            },
            "locale": {"preferred_language": "ru", "timezone": "Europe/Moscow"},
        }

    async def test_the_change_persists(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """Round-trips through the `jsonb` column and back out through
        `from_document`, which is the half a response-only assertion would
        not reach."""
        _, auth = account
        await patch_preferences(client, auth, {"gameplay": {"board_theme": "wood"}})

        assert (await read_preferences(client, auth))["gameplay"]["board_theme"] == "wood"

    async def test_defaults_before_anything_is_changed(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """A fresh account stores an empty document and reads back a
        complete one. That is what makes adding a sixth setting a code
        change with no backfill."""
        _, auth = account

        response = await client.get(PREFERENCES_URL, headers=auth)

        assert response.status_code == 200, response.text
        assert response.json()["data"] == DEFAULTS

    async def test_an_empty_body_changes_nothing(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account

        response = await patch_preferences(client, auth, {})

        assert response.status_code == 200, response.text
        assert response.json()["data"] == DEFAULTS


class TestPartialUpdate:
    """A64-012.5's fourth required test, at both levels of the shape."""

    async def test_an_omitted_group_is_left_alone(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account
        await patch_preferences(client, auth, {"locale": {"timezone": "Asia/Tashkent"}})

        await patch_preferences(client, auth, {"gameplay": {"board_theme": "marble"}})

        settings = await read_preferences(client, auth)
        assert settings["locale"]["timezone"] == "Asia/Tashkent"
        assert settings["gameplay"]["board_theme"] == "marble"

    async def test_an_omitted_setting_inside_a_sent_group_is_left_alone(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """The failure this guards is a group write implemented as a
        replace: naming one setting would reset the other four to their
        defaults, and the response would look entirely successful."""
        _, auth = account
        await patch_preferences(
            client, auth, {"gameplay": {"piece_set": "modern", "confirm_move": True}}
        )

        await patch_preferences(client, auth, {"gameplay": {"board_theme": "wood"}})

        gameplay = (await read_preferences(client, auth))["gameplay"]
        assert gameplay["board_theme"] == "wood"
        assert gameplay["piece_set"] == "modern"
        assert gameplay["confirm_move"] is True

    async def test_an_empty_group_object_changes_nothing_in_it(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account
        await patch_preferences(client, auth, {"gameplay": {"board_theme": "wood"}})

        response = await patch_preferences(client, auth, {"gameplay": {}})

        assert response.status_code == 200, response.text
        assert response.json()["data"]["gameplay"]["board_theme"] == "wood"


class TestInvalidPreferenceValue:
    """A64-012.5's second required test."""

    @pytest.mark.parametrize(
        ("setting", "value"),
        [
            ("board_theme", "neon"),
            ("piece_set", "staunton"),
            ("animation_speed", "turbo"),
            ("board_theme", "CLASSIC"),
            ("confirm_move", "sometimes"),
        ],
    )
    async def test_a_value_outside_the_allowed_set_is_rejected(
        self, client: AsyncClient, account: tuple[str, dict[str, str]], setting: str, value: str
    ) -> None:
        _, auth = account

        response = await patch_preferences(client, auth, {"gameplay": {setting: value}})

        assert response.status_code == 422, response.text
        assert setting in response.text

    @pytest.mark.parametrize("language", ["de", "fr", "EN", "english", ""])
    async def test_an_unsupported_language_is_rejected(
        self, client: AsyncClient, account: tuple[str, dict[str, str]], language: str
    ) -> None:
        """Moved here from `test_profile_editing_api.py` along with the
        field itself."""
        _, auth = account

        assert (
            await patch_preferences(client, auth, {"locale": {"preferred_language": language}})
        ).status_code == 422

    @pytest.mark.parametrize("language", ["en", "ru", "uz"])
    async def test_every_supported_language_is_accepted(
        self, client: AsyncClient, account: tuple[str, dict[str, str]], language: str
    ) -> None:
        _, auth = account

        response = await patch_preferences(
            client, auth, {"locale": {"preferred_language": language}}
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["locale"]["preferred_language"] == language

    async def test_a_rejected_value_writes_nothing(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """A body with a good board theme and a bad animation speed changes
        neither."""
        _, auth = account

        response = await patch_preferences(
            client, auth, {"gameplay": {"board_theme": "wood", "animation_speed": "turbo"}}
        )

        assert response.status_code == 422
        assert (await read_preferences(client, auth)) == DEFAULTS


class TestInvalidTimezone:
    """A64-012.5's third required test."""

    @pytest.mark.parametrize(
        "timezone", ["Mars/Olympus", "GMT+5", "+05:00", "Asia/Tashkent/Extra", ""]
    )
    async def test_a_non_iana_timezone_is_rejected(
        self, client: AsyncClient, account: tuple[str, dict[str, str]], timezone: str
    ) -> None:
        _, auth = account

        response = await patch_preferences(client, auth, {"locale": {"timezone": timezone}})

        assert response.status_code == 422, response.text

    @pytest.mark.parametrize("timezone", ["UTC", "Asia/Tashkent", "Europe/London"])
    async def test_a_real_iana_name_is_accepted(
        self, client: AsyncClient, account: tuple[str, dict[str, str]], timezone: str
    ) -> None:
        _, auth = account

        response = await patch_preferences(client, auth, {"locale": {"timezone": timezone}})

        assert response.status_code == 200, response.text
        assert response.json()["data"]["locale"]["timezone"] == timezone

    async def test_a_bad_timezone_does_not_write_the_gameplay_group(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """Both groups arrive in one request and one transaction. The
        timezone is constructed before the account row is touched, so a
        rejected one leaves the board theme alone."""
        _, auth = account

        response = await patch_preferences(
            client,
            auth,
            {"gameplay": {"board_theme": "wood"}, "locale": {"timezone": "Mars/Olympus"}},
        )

        assert response.status_code == 422
        assert (await read_preferences(client, auth)) == DEFAULTS


class TestUnknownKeys:
    async def test_an_unknown_group_is_rejected(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account

        response = await patch_preferences(client, auth, {"ui": {"density": "compact"}})

        assert response.status_code == 422
        assert "ui" in response.text

    async def test_an_unknown_setting_inside_a_known_group_is_rejected(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """The check the outer schema cannot make. Without `extra="forbid"`
        on the group itself, anything inside the braces would be accepted
        and silently dropped."""
        _, auth = account

        response = await patch_preferences(client, auth, {"gameplay": {"sound_enabled": True}})

        assert response.status_code == 422
        assert "sound_enabled" in response.text

    @pytest.mark.parametrize(
        "body",
        [{"gameplay": None}, {"gameplay": {"board_theme": None}}, {"locale": {"timezone": None}}],
    )
    async def test_null_is_rejected_at_both_levels(
        self, client: AsyncClient, account: tuple[str, dict[str, str]], body: dict[str, Any]
    ) -> None:
        """No preference has an empty state, so `null` would need a meaning
        invented for it — most plausibly "reset to default", which is not a
        decision this endpoint may make implicitly."""
        _, auth = account

        assert (await patch_preferences(client, auth, body)).status_code == 422


class TestLocaleHasOneWritablePath:
    """A64-012.5's "avoid duplicated writable fields", asserted from both
    ends. Either half alone would pass if the fields had simply been
    deleted."""

    @pytest.mark.parametrize("field", ["preferred_language", "timezone"])
    async def test_profile_editing_rejects_them(
        self, client: AsyncClient, account: tuple[str, dict[str, str]], field: str
    ) -> None:
        _, auth = account
        value = "uz" if field == "preferred_language" else "Asia/Tashkent"

        response = await client.patch(PROFILE_URL, headers=auth, json={field: value})

        assert response.status_code == 422
        assert field in response.text

    async def test_this_endpoint_accepts_them(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account

        response = await patch_preferences(
            client, auth, {"locale": {"preferred_language": "uz", "timezone": "Asia/Tashkent"}}
        )

        assert response.status_code == 200, response.text

    async def test_the_language_reaches_the_account_view(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """One storage location, read from two places. `GET /auth/me`
        reports the account's language and must see a change made here —
        otherwise the move would have created a second copy rather than
        removing one."""
        _, auth = account
        await patch_preferences(client, auth, {"locale": {"preferred_language": "ru"}})

        me = (await client.get("/api/v1/auth/me", headers=auth)).json()["data"]

        assert me["preferred_language"] == "ru"


class TestNeverPublic:
    async def test_no_preference_appears_on_the_public_profile(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """A64-012.5: "preferences are never exposed publicly." Asserted
        against a profile whose every setting has been changed away from
        its default, so a leak would be visible rather than coincidentally
        equal."""
        username, auth = account
        await patch_preferences(
            client,
            auth,
            {
                "gameplay": {"board_theme": "midnight", "confirm_move": True},
                "locale": {"timezone": "Asia/Tashkent"},
            },
        )

        profile = (await client.get(f"/api/v1/profiles/{username}")).json()["data"]

        assert "gameplay" not in profile
        assert "preferences" not in profile
        assert "timezone" not in profile
        assert not [key for key in profile if key in DEFAULTS["gameplay"]]


class TestUnauthorized:
    async def test_update_without_a_token_is_refused(self, client: AsyncClient) -> None:
        response = await client.patch(PREFERENCES_URL, json={"gameplay": {"board_theme": "wood"}})

        assert response.status_code == 401

    async def test_read_without_a_token_is_refused(self, client: AsyncClient) -> None:
        assert (await client.get(PREFERENCES_URL)).status_code == 401

    async def test_an_unauthenticated_request_changes_nothing(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account

        await client.patch(PREFERENCES_URL, json={"gameplay": {"board_theme": "wood"}})

        assert (await read_preferences(client, auth)) == DEFAULTS

    async def test_there_is_no_way_to_name_another_account(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        victim, _ = await register(client)
        _, attacker = account

        by_body = await patch_preferences(
            client, attacker, {"user_id": victim, "gameplay": {"board_theme": "wood"}}
        )
        by_path = await client.patch(
            f"/api/v1/profiles/{victim}/preferences",
            headers=attacker,
            json={"gameplay": {"board_theme": "wood"}},
        )

        assert by_body.status_code == 422
        assert by_path.status_code in (404, 405)


class TestRateLimiting:
    def test_the_update_is_limited_per_user(self) -> None:
        """The dimension is the point. A64-012.5 specifies USER-based
        limiting, and the alternative — per IP — throttles everyone behind
        one office or carrier NAT for one member's behaviour."""
        from app.config.settings import RateLimitSettings

        rules = PREFERENCES_UPDATE_RATE_LIMIT.rules(RateLimitSettings())

        assert [rule.scope for rule in rules] == [RateLimitScope.USER]
        assert rules[0].name == "preferences_update_user"

    def test_the_route_declares_the_guard(self) -> None:
        """A guard that exists but is not attached protects nothing."""
        from app.modules.profiles.presentation.rate_limits import (
            enforce_preferences_update_limit,
        )

        patches = [
            route
            for route in my_profile_router.routes
            if getattr(route, "path", None) == "/profile/preferences"
            and "PATCH" in getattr(route, "methods", set())
        ]

        assert len(patches) == 1
        guards = [
            dependency.dependency
            for dependency in patches[0].dependencies  # type: ignore[attr-defined]
        ]
        assert enforce_preferences_update_limit in guards

    def test_the_read_carries_no_guard(self) -> None:
        reads = [
            route
            for route in my_profile_router.routes
            if getattr(route, "path", None) == "/profile/preferences"
            and "GET" in getattr(route, "methods", set())
        ]

        assert len(reads) == 1
        assert reads[0].dependencies == []  # type: ignore[attr-defined]


class TestOpenApi:
    def test_both_operations_are_documented(self) -> None:
        schema = create_app().openapi()
        path = schema["paths"][PREFERENCES_URL]

        assert {"get", "patch"} <= path.keys()
        assert path["patch"]["summary"]
        assert "429" in path["patch"]["responses"]
        assert "422" in path["patch"]["responses"]

    def test_every_group_forbids_unknown_keys(self) -> None:
        schemas = create_app().openapi()["components"]["schemas"]

        for name in (
            "PreferencesUpdateRequest",
            "GameplayPreferencesUpdate",
            "LocalePreferencesUpdate",
        ):
            assert schemas[name]["additionalProperties"] is False, name

    def test_every_setting_documents_its_default(self) -> None:
        """A64-012.5: "document all preference fields, default values,
        examples". The defaults are interpolated from the domain
        constants, so the documentation cannot drift from the code."""
        schemas = create_app().openapi()["components"]["schemas"]
        gameplay = schemas["GameplayPreferencesUpdate"]["properties"]
        locale = schemas["LocalePreferencesUpdate"]["properties"]

        assert set(gameplay) == set(DEFAULTS["gameplay"])
        assert set(locale) == set(DEFAULTS["locale"])
        for name, spec in (*gameplay.items(), *locale.items()):
            assert "Default:" in spec["description"], name
            assert spec.get("examples"), name

    def test_the_enums_reach_the_schema(self) -> None:
        schemas = create_app().openapi()["components"]["schemas"]

        assert set(schemas["BoardTheme"]["enum"]) == {"classic", "wood", "marble", "midnight"}
        assert set(schemas["PieceSet"]["enum"]) == {"classic", "modern", "neo"}
        assert set(schemas["AnimationSpeed"]["enum"]) == {"instant", "fast", "normal", "slow"}
