"""Privacy settings end to end — real PostgreSQL, the real composition
root.

A64-012.4 asks for essential tests only and names three: a successful
update, the public profile hiding private fields, and an unauthorised
update. All three are here.

What else is here is the small set of properties that would be *silently*
wrong rather than loudly broken, which on a privacy control means wrong in
the direction of publishing something:

  - **PATCH semantics** — sending one flag must not reset the other four.
    The flag that would be reset is `show_last_seen`, whose default is
    *off*, so the failure is "the platform re-published a person's schedule
    because they changed an unrelated setting";
  - **a hidden field is `null`, not a placeholder** — and for statistics
    specifically, not zeroes, which would read as a beginner rather than as
    a private player;
  - **hidden and never-set are indistinguishable** — a hidden country
    returns exactly what a player who never set one returns, so the
    response cannot be used to detect that something is being hidden;
  - **the flags never appear on the public profile** — the settings are the
    owner's, and publishing them would answer the question they exist to
    decline;
  - **ratings stay visible** when statistics are hidden (UP-5).

The rate limiter is replaced by `AllowAllRateLimiter`, as it is in every
other endpoint suite — the limiter's own behaviour is
`tests/contract/test_rate_limiter.py`'s subject. That the guard is
*attached* to `PATCH /profile/privacy` at all is asserted here, since a
missing guard is exactly the kind of thing a rate-limiter test cannot see.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_factory import create_app
from app.core.rate_limiting import RateLimitScope
from app.modules.profiles.presentation.rate_limits import (
    PRIVACY_UPDATE_RATE_LIMIT,
    enforce_privacy_update_limit,
)
from app.modules.profiles.presentation.self_router import my_profile_router
from tests.contract.contract_app import build_contract_app, contract_client

PRIVACY_URL = "/api/v1/profile/privacy"
PROFILE_URL = "/api/v1/profile"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"

#: The five flags and the defaults A64-012.4 specifies. Spelled out here
#: rather than imported from `domain/privacy.py` on purpose: importing the
#: constants would make this assert that the code agrees with itself, which
#: is the one thing it cannot usefully prove. These are the numbers from the
#: brief, typed in by hand.
DEFAULTS = {
    "show_country": True,
    "show_statistics": True,
    # A64-013.2 widened three of the five to `VisibilityLevel`. Each value
    # is the widening of the boolean it replaced — `false -> nobody`,
    # `true -> everyone` — which is what makes the migration lossless, and
    # asserting the *values* here is what would catch a conversion that
    # moved somebody's setting.
    "last_seen": "nobody",
    "online_status": "everyone",
    "activity": "everyone",
    # The deprecated booleans, still returned so clients written before
    # A64-013.2 keep working. Derived from the three above rather than
    # stored, so they cannot disagree.
    "show_last_seen": False,
    "show_online_status": True,
    "show_activity": True,
}

#: What a client may *send*. The same keys as `DEFAULTS`, because every
#: setting is writable in both spellings — the request and the response
#: happen to have the same shape, and asserting them separately is what
#: would catch one of them drifting.
SETTABLE = set(DEFAULTS)


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app over the test's rolled-back transaction.

    No override on any privacy service, mapper or schema — the graph under
    test is the one that ships. Only `lifespan`'s state is stood in for
    (`tests/contract/contract_app.py`)."""
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
    token = signed_in.json()["data"]["access_token"]
    return username, {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def account(client: AsyncClient) -> tuple[str, dict[str, str]]:
    return await register(client)


async def patch_privacy(client: AsyncClient, auth: dict[str, str], body: dict[str, Any]) -> Any:
    return await client.patch(PRIVACY_URL, headers=auth, json=body)


class TestSuccessfulUpdate:
    async def test_every_setting_can_be_closed(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """Everything off, in the current spelling.

        A64-013.2 changed what "off" means for three of the five: `false`
        for the two that stayed boolean, `nobody` for the three that became
        audience-valued. The deprecated booleans are not sent — sending both
        spellings for one setting is a `422` by design.
        """
        _, auth = account

        response = await patch_privacy(
            client,
            auth,
            {
                "show_country": False,
                "show_statistics": False,
                "last_seen": "nobody",
                "online_status": "nobody",
                "activity": "nobody",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"] == {
            "show_country": False,
            "show_statistics": False,
            "last_seen": "nobody",
            "online_status": "nobody",
            "activity": "nobody",
            # Derived, and `false` for all three — which is the honest
            # answer to the question the deprecated field asks.
            "show_last_seen": False,
            "show_online_status": False,
            "show_activity": False,
        }

    async def test_a_friends_only_setting_is_stored_and_reads_as_not_public(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """The value a boolean could not express — A64-013.2's whole reason.

        `friends` is accepted and stored from this release even though no
        friendship exists to satisfy it, so a player can set what they mean
        today. The deprecated boolean reads `false`, which is correct: it
        asks whether *anybody* may see the field.
        """
        _, auth = account

        response = await patch_privacy(client, auth, {"online_status": "friends"})

        assert response.status_code == 200, response.text
        assert response.json()["data"]["online_status"] == "friends"
        assert response.json()["data"]["show_online_status"] is False

    async def test_the_deprecated_boolean_still_works(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """**The non-breaking guarantee**, asserted rather than claimed.

        A client written before A64-013.2 sends `show_last_seen: true` and
        must still be understood. It widens to `everyone`, which is what
        `true` always meant.
        """
        _, auth = account

        response = await patch_privacy(client, auth, {"show_last_seen": True})

        assert response.status_code == 200, response.text
        assert response.json()["data"]["last_seen"] == "everyone"
        assert response.json()["data"]["show_last_seen"] is True

    async def test_sending_both_spellings_for_one_setting_is_refused(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """Two intentions for one column, and no correct way to pick.

        Precedence would be a coin flip from the client's side; on a privacy
        endpoint that means a caller believing it hid something it
        published.
        """
        _, auth = account

        response = await patch_privacy(
            client, auth, {"last_seen": "friends", "show_last_seen": True}
        )

        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    async def test_the_change_persists(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account
        await patch_privacy(client, auth, {"show_country": False})

        settings = (await client.get(PRIVACY_URL, headers=auth)).json()["data"]

        assert settings["show_country"] is False

    async def test_defaults_before_anything_is_changed(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """A fresh account is on the platform defaults, including the one
        that is off — `show_last_seen`. Reading them from the database
        rather than from a Python default is the point: the columns carry
        `server_default`, and registration never mentions them."""
        _, auth = account

        response = await client.get(PRIVACY_URL, headers=auth)

        assert response.status_code == 200, response.text
        assert response.json()["data"] == DEFAULTS

    async def test_one_flag_does_not_reset_the_others(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """The failure this guards is specific and silent: a PATCH
        implemented as a whole-object replace would set `show_last_seen`
        back to its default of `false` — or, worse, a client sending
        `{"show_last_seen": true}` once would find it reset by the next
        unrelated change."""
        _, auth = account
        await patch_privacy(client, auth, {"show_last_seen": True})

        await patch_privacy(client, auth, {"show_country": False})

        settings = (await client.get(PRIVACY_URL, headers=auth)).json()["data"]
        assert settings["show_last_seen"] is True
        assert settings["show_country"] is False
        assert settings["show_statistics"] is True

    async def test_an_empty_body_changes_nothing(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account

        response = await patch_privacy(client, auth, {})

        assert response.status_code == 200, response.text
        assert response.json()["data"] == DEFAULTS


class TestPublicProfileRespectsPrivacy:
    """A64-012.4's second required test, in the four forms it takes."""

    async def test_country_is_null_when_hidden(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        username, auth = account
        await client.patch(PROFILE_URL, headers=auth, json={"country": "UZ"})

        visible = await client.get(f"/api/v1/profiles/{username}")
        assert visible.json()["data"]["country"] == "UZ"

        await patch_privacy(client, auth, {"show_country": False})

        hidden = await client.get(f"/api/v1/profiles/{username}")
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()["data"]["country"] is None

    async def test_a_hidden_country_looks_exactly_like_an_unset_one(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """The response must not let a caller tell the two apart. If it
        could, the setting would announce that something is being hidden —
        which is the question hiding it declines to answer."""
        username, auth = account
        await client.patch(PROFILE_URL, headers=auth, json={"country": "UZ"})
        await patch_privacy(client, auth, {"show_country": False})
        hiding = (await client.get(f"/api/v1/profiles/{username}")).json()["data"]

        other_username, _ = await register(client)
        never_set = (await client.get(f"/api/v1/profiles/{other_username}")).json()["data"]

        assert hiding["country"] is never_set["country"] is None
        assert hiding.keys() == never_set.keys()

    async def test_statistics_are_null_when_hidden_not_zeroed(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """`null`, never a zeroed record. A zeroed record is what a genuine
        beginner has, so publishing one for a player who opted out would
        misinform the opponent deciding whether to accept a challenge —
        which is worse than publishing nothing."""
        username, auth = account
        shown = (await client.get(f"/api/v1/profiles/{username}")).json()["data"]
        assert shown["statistics"]["games_played"] == 0

        await patch_privacy(client, auth, {"show_statistics": False})

        hidden = (await client.get(f"/api/v1/profiles/{username}")).json()["data"]
        assert hidden["statistics"] is None

    async def test_ratings_stay_visible_when_statistics_are_hidden(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """UP-5, and the reason `show_statistics` is not `show_ratings`: a
        rating is what pairing is computed from, and a player who could
        hide theirs while accepting rated games would be sandbagging with
        the platform's help."""
        username, auth = account
        await patch_privacy(client, auth, {"show_statistics": False})

        profile = (await client.get(f"/api/v1/profiles/{username}")).json()["data"]

        assert profile["statistics"] is None
        assert profile["ratings"]["blitz"]["rating"] > 0

    async def test_the_flags_themselves_never_appear_publicly(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """No `show_*` key anywhere on the public profile, whatever the
        settings are. "This player hides their country" is itself the
        disclosure."""
        username, auth = account
        await patch_privacy(client, auth, dict.fromkeys(DEFAULTS, False))

        profile = (await client.get(f"/api/v1/profiles/{username}")).json()["data"]

        assert not [key for key in profile if key.startswith("show_")]
        assert "visibility" not in profile
        assert "privacy" not in profile

    async def test_the_owners_own_views_are_never_redacted(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """Privacy governs what *strangers* see. A settings screen that hid
        a country from the person who typed it would be unusable, and
        `GET /profile/me` is that screen's data source."""
        _, auth = account
        await client.patch(PROFILE_URL, headers=auth, json={"country": "UZ"})
        await patch_privacy(client, auth, {"show_country": False})

        mine = (await client.get(f"{PROFILE_URL}/me", headers=auth)).json()["data"]

        assert mine["country"] == "UZ"


class TestUnauthorized:
    """A64-012.4's third required test. Both verbs, both failure shapes."""

    async def test_update_without_a_token_is_refused(self, client: AsyncClient) -> None:
        response = await client.patch(PRIVACY_URL, json={"show_country": False})

        assert response.status_code == 401

    async def test_read_without_a_token_is_refused(self, client: AsyncClient) -> None:
        assert (await client.get(PRIVACY_URL)).status_code == 401

    async def test_a_garbage_token_is_refused(self, client: AsyncClient) -> None:
        response = await client.patch(
            PRIVACY_URL,
            headers={"Authorization": "Bearer not.a.token"},
            json={"show_country": False},
        )

        assert response.status_code == 401

    async def test_an_unauthenticated_request_changes_nothing(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """The 401 must be a refusal, not a rejected response to a write
        that already happened."""
        _, auth = account

        await client.patch(PRIVACY_URL, json={"show_country": False})

        assert (await client.get(PRIVACY_URL, headers=auth)).json()["data"] == DEFAULTS

    async def test_there_is_no_way_to_name_another_account(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """The ownership guarantee is structural: the account comes from
        the token, so a body naming somebody else is an unknown field
        rather than a target. This asserts the absence of the endpoint that
        would break it — a `/profiles/{username}/privacy` would be the
        shape to worry about."""
        victim_username, _ = await register(client)
        _, attacker = account

        by_body = await patch_privacy(
            client, attacker, {"user_id": victim_username, "show_country": False}
        )
        by_path = await client.patch(
            f"/api/v1/profiles/{victim_username}/privacy",
            headers=attacker,
            json={"show_country": False},
        )

        assert by_body.status_code == 422
        assert by_path.status_code in (404, 405)


class TestValidation:
    async def test_unknown_fields_are_rejected(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """Rejected, not ignored. A client that believed it had hidden its
        email would act as though the field were private."""
        _, auth = account

        response = await patch_privacy(client, auth, {"show_email": False})

        assert response.status_code == 422
        assert "show_email" in response.text

    async def test_a_null_flag_is_rejected(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        _, auth = account

        response = await patch_privacy(client, auth, {"show_country": None})

        assert response.status_code == 422
        assert "show_country" in response.text

    async def test_a_non_boolean_flag_is_rejected(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """`"maybe"` rather than `"false"`, because Pydantic's lax mode —
        which every request schema on this platform runs in — coerces the
        recognised boolean spellings (`"true"`, `"no"`, `0`, `1`) rather
        than rejecting them. That is platform-wide behaviour and not
        something this endpoint should differ on; what it must reject is a
        value with no boolean reading at all."""
        _, auth = account

        assert (await patch_privacy(client, auth, {"show_country": "maybe"})).status_code == 422

    async def test_a_rejected_request_writes_nothing(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """A body with one good flag and one unknown one changes neither."""
        _, auth = account

        response = await patch_privacy(client, auth, {"show_country": False, "show_email": False})

        assert response.status_code == 422
        assert (await client.get(PRIVACY_URL, headers=auth)).json()["data"] == DEFAULTS

    async def test_profile_editing_cannot_reach_a_privacy_flag(
        self, client: AsyncClient, account: tuple[str, dict[str, str]]
    ) -> None:
        """The two surfaces are separate ports and separate schemas.
        `PATCH /profile` must not accept `show_country` — otherwise the
        rate limit on the privacy endpoint would be bypassable by using
        the other one."""
        _, auth = account

        response = await client.patch(PROFILE_URL, headers=auth, json={"show_country": False})

        assert response.status_code == 422


class TestRateLimiting:
    def test_the_update_is_limited_per_user(self) -> None:
        """Asserted here rather than by sending twenty-one requests: the
        limiter's own behaviour has its own suite, and what this endpoint
        can get wrong is having no guard at all — or having one on the
        wrong dimension.

        A64-012.4 shipped this per IP because `RateLimitScope` had nothing
        better; A64-012.5 added `USER` and A64-012.6 migrated it, so a
        shared office or carrier NAT no longer throttles one player for
        another's behaviour."""
        from app.config.settings import RateLimitSettings

        rules = PRIVACY_UPDATE_RATE_LIMIT.rules(RateLimitSettings())

        assert [rule.scope for rule in rules] == [RateLimitScope.USER]
        assert rules[0].name == "privacy_update_user"

    def test_the_route_declares_it(self) -> None:
        """A guard that exists but is not attached protects nothing.

        Asserted against the router's own declaration rather than against
        the composed `app.routes`, which this FastAPI version keeps as
        nested `_IncludedRouter` objects — walking that tree would be a
        test of framework internals, and the declaration site is where the
        mistake would actually be made.
        """
        patches = [
            route
            for route in my_profile_router.routes
            if getattr(route, "path", None) == "/profile/privacy"
            and "PATCH" in getattr(route, "methods", set())
        ]

        assert len(patches) == 1
        guards = [
            dependency.dependency
            for dependency in patches[0].dependencies  # type: ignore[attr-defined]
        ]
        # The wrapper, not the guard itself: a USER-scoped rule needs the
        # authenticated principal, which only a module presentation layer
        # may resolve. See `profiles.presentation.rate_limits`.
        assert enforce_privacy_update_limit in guards

    def test_the_read_carries_no_guard(self) -> None:
        """Deliberate: a settings screen loads this on every visit, and it
        changes nothing. See `profiles.presentation.rate_limits`."""
        reads = [
            route
            for route in my_profile_router.routes
            if getattr(route, "path", None) == "/profile/privacy"
            and "GET" in getattr(route, "methods", set())
        ]

        assert len(reads) == 1
        assert reads[0].dependencies == []  # type: ignore[attr-defined]


class TestOpenApi:
    def test_both_operations_are_documented(self) -> None:
        schema = create_app().openapi()
        path = schema["paths"][PRIVACY_URL]

        assert {"get", "patch"} <= path.keys()
        assert path["patch"]["summary"]
        assert "429" in path["patch"]["responses"]

    def test_the_request_body_forbids_unknown_fields(self) -> None:
        schema = create_app().openapi()
        body = schema["components"]["schemas"]["PrivacySettingsUpdateRequest"]

        assert body["additionalProperties"] is False
        assert set(body["properties"]) == SETTABLE
        for flag in DEFAULTS:
            assert body["properties"][flag]["description"]
