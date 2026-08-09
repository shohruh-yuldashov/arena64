"""The browser-session origin and cookie policy — A64-024.2H §13.

Six tests over the **real** `enforce_trusted_origin` and the **real**
`BrowserSessionSettings`. What they protect is a deployment property that no
other test can see: which origins may hold a session, and whether the cookie
that carries it is host-only.

## Why this is worth testing rather than documenting

Every guarantee in `specs/admin.md` §6.2 rests on two configuration facts —
the trusted-origin list is exact, and the cookie has no `Domain`. Both are
one careless edit away from being wrong, and both fail *open*: a wildcard
origin and a `.arena64.gg` cookie each produce a working system that has
quietly merged the player session with the administrator's.
"""

from datetime import UTC, datetime

import pytest
from fastapi import Request

from app.config.environment import Environment
from app.config.settings import BrowserSessionSettings
from app.modules.auth.presentation.browser_csrf import (
    UntrustedOrigin,
    enforce_trusted_origin,
)

PLAYER = "https://arena64.gg"
ADMIN = "https://admin.arena64.gg"


def _request(origin: str | None, *, referer: str | None = None) -> Request:
    """One inbound request carrying an `Origin` (or not).

    Built from a raw ASGI scope rather than through a `TestClient`, because
    what is under test is the header check itself — routing, dependencies
    and a session would all be machinery between the assertion and the
    behaviour.
    """
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if referer is not None:
        headers.append((b"referer", referer.encode()))
    return Request({"type": "http", "method": "POST", "headers": headers})


def _deployed(*origins: str) -> BrowserSessionSettings:
    return BrowserSessionSettings(trusted_origins=tuple(origins))


class TestTheTrustedOriginList:
    def test_both_front_ends_are_admitted_and_nothing_else_is(self) -> None:
        """§13.1 and §13.2 — the list is exact.

        Two front ends, two deployments (AD-04), and the console must be on
        the list in its own right: `admin.arena64.gg` is not covered by
        `arena64.gg` being there, which is the mistake that presents as a
        console failing at login with `403` on the day it ships.
        """
        settings = _deployed(PLAYER, ADMIN)

        enforce_trusted_origin(_request(PLAYER), settings)
        enforce_trusted_origin(_request(ADMIN), settings)

        for refused in (
            "https://evil.example",
            # A sibling host on the same registrable domain. Same-site for
            # `SameSite` purposes, and still not a front end of this
            # platform — the two questions are unrelated and this is the
            # one that decides.
            "https://staging.arena64.gg",
            # The API's own host. No browser ever claims to be one, and a
            # deployment that listed it would have allowed nothing while
            # believing otherwise.
            "https://api.arena64.gg",
            # Prefix and suffix confusion, both directions.
            "https://arena64.gg.evil.example",
            "https://notarena64.gg",
        ):
            with pytest.raises(UntrustedOrigin):
                enforce_trusted_origin(_request(refused), settings)

    def test_a_missing_origin_is_refused_rather_than_defaulted(self) -> None:
        """An absent `Origin` must not pass.

        Browsers send it on state-changing requests; something that does
        not is not a browser this contract covers. `Referer` is the
        documented fallback, and only its origin component is read.
        """
        settings = _deployed(PLAYER, ADMIN)

        with pytest.raises(UntrustedOrigin):
            enforce_trusted_origin(_request(None), settings)

        # The fallback still works, and still has to match.
        enforce_trusted_origin(_request(None, referer=f"{ADMIN}/users"), settings)
        with pytest.raises(UntrustedOrigin):
            enforce_trusted_origin(_request(None, referer="https://evil.example/x"), settings)

    def test_a_wildcard_is_a_literal_and_never_a_pattern(self) -> None:
        """§13.3 — `*` must not become "anything".

        The check is set membership, so a wildcard configured by somebody
        expecting glob semantics admits **only** the literal string `*` —
        which no browser sends. Asserted so that a future change to
        pattern matching has to break a test rather than quietly widen the
        list.
        """
        settings = _deployed("*")

        for origin in (PLAYER, ADMIN, "https://evil.example"):
            with pytest.raises(UntrustedOrigin):
                enforce_trusted_origin(_request(origin), settings)


class TestTheCookieIsHostOnly:
    def test_the_settings_expose_no_domain_at_all(self) -> None:
        """§13.4 — the property the whole isolation model rests on.

        A cookie with `Domain=.arena64.gg` would be sent to every host under
        it, merging the player session and the administrator's into one
        credential. The strongest available assertion is about **absence**:
        `BrowserSessionSettings` has no domain field, so there is no
        configuration through which one could be set, and no call site that
        could pass one.
        """
        fields = set(BrowserSessionSettings.model_fields)
        assert not fields & {"domain", "cookie_domain"}

        # The attributes that *are* configured, at their documented values.
        settings = BrowserSessionSettings()
        assert settings.same_site == "lax"
        assert settings.cookie_path == "/api/v1/auth/browser"
        assert settings.cookie_name == "arena64_refresh"

    def test_a_deployed_tier_always_marks_the_cookie_secure(self) -> None:
        """A refresh cookie on plaintext HTTP is a credential handed to
        anybody on the path, so `Secure` is not configurable off in a
        deployed tier — only relaxed for local development."""
        settings = BrowserSessionSettings()

        assert settings.secure_for(Environment.PRODUCTION) is True
        assert settings.secure_for(Environment.STAGING) is True
        assert settings.secure_for(Environment.LOCAL) is False


class TestTheEmptyListIsOnlyForLocalDevelopment:
    def test_an_empty_list_disables_the_check_and_is_refused_in_production(self) -> None:
        """The one dangerous state, and the guard that stops it shipping.

        An empty list makes `enforce_trusted_origin` a no-op — correct in
        `local`, where the Vite proxy makes every app same-origin and there
        is no cross-origin case to allow. `Settings` refuses to start a
        deployed tier in that state, which is what keeps "no configuration"
        from meaning "no CSRF check".
        """
        permissive = BrowserSessionSettings()
        assert permissive.trusted_origins == ()

        # A no-op: nothing raises, whatever is presented.
        enforce_trusted_origin(_request("https://evil.example"), permissive)
        enforce_trusted_origin(_request(None), permissive)

        # And the composed settings refuse it outside local — asserted
        # through the message, because the failure has to be readable by
        # whoever is deploying at the time.
        from pydantic import ValidationError

        from app.config.settings import Settings

        with pytest.raises((ValidationError, ValueError)) as refused:
            Settings.model_validate(
                {
                    "environment": Environment.PRODUCTION,
                    **_minimal_production_sections(),
                }
            )
        assert "TRUSTED_ORIGINS" in str(refused.value) or "trusted_origins" in str(refused.value)


def _minimal_production_sections() -> dict[str, object]:
    """Whatever else `Settings` needs, so the assertion is about origins.

    Deliberately thin: if the composed model grows a new required section
    this test fails loudly rather than silently asserting the wrong
    validator, which is the failure a `pytest.raises` with no message check
    would hide.
    """
    return {"browser_session": BrowserSessionSettings(trusted_origins=())}


NOW = datetime(2026, 8, 9, tzinfo=UTC)
