"""The server-side half of the browser session's CSRF defence — A64-020.2.

## Why cookie authentication needs this and bearer authentication does not

A bearer token is attached by the code that holds it. An attacker's page
can cause a request to this API, but it cannot read our memory to find the
token, so the forged request simply arrives unauthenticated.

A **cookie** is attached by the browser, to any request the browser makes
to that origin — including one an attacker's page caused. That is the whole
of CSRF, and it applies to exactly the endpoints that authenticate by
cookie: `refresh` and `logout`. Nothing else on this platform uses one, so
nothing else is guarded here.

## Two layers, because one is a promise made by somebody else

**`SameSite=Lax`** (see `BrowserSessionSettings`) stops a cross-site
`POST` from carrying the cookie at all. It is the stronger of the two and
it is not sufficient, because it is enforced by the *browser*: a client
that predates it, or that does not implement it correctly, simply sends
the cookie.

**This module** is the half the server controls. A state-changing browser
request must present an `Origin` — or, for the browsers that omit it, a
`Referer` — that this deployment recognises. An unrecognised or absent
origin is refused before any session is touched.

## Why `local` and `test` are exempt

`trusted_origins` is empty there and required everywhere else (enforced on
`Settings`). The Vite dev server proxies `/api` to this process, so the
browser sees one origin and there is no cross-origin case to allow —
demanding a configured list would mean every developer maintaining one to
describe a situation that cannot arise. In a deployed tier the list is
mandatory, so "empty" can never silently mean "allow everything".
"""

from typing import Annotated, ClassVar
from urllib.parse import urlsplit

from fastapi import Depends, Request

from app.api.deps import SettingsDep
from app.config.settings import BrowserSessionSettings
from app.core.error_codes import ErrorCode
from app.core.exceptions import PermissionDeniedError


class UntrustedOrigin(PermissionDeniedError):
    """The request did not come from a web origin this deployment knows.

    A `403` rather than a `401`: the caller may well hold a valid session,
    and that is precisely the case being refused — the credential is fine
    and the *request* is not one this user made deliberately.

    The message names no origin, trusted or presented. Echoing either would
    turn a refusal into a way to enumerate the deployment's front ends.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.PERMISSION_DENIED


def enforce_trusted_origin(request: Request, settings: BrowserSessionSettings) -> None:
    """Raises `UntrustedOrigin` unless this request came from a known front end.

    Reads `Origin` first and falls back to `Referer`. `Origin` is what
    browsers send on cross-origin and same-origin state-changing requests;
    `Referer` is the older signal, and only its origin component is used —
    the path in a `Referer` is somebody's browsing history and this
    function has no business reading it.

    **An empty trusted list means "not deployed"**, and is allowed only
    because `Settings` refuses to start a production-like tier with one.
    """
    if not settings.trusted_origins:
        return

    presented = _origin_of(request)
    if presented is None or presented not in settings.trusted_origins:
        # No detail, deliberately: which of "absent" and "not on the list"
        # occurred is information about the deployment, and the caller's
        # response to both is the same.
        raise UntrustedOrigin("This request did not come from a recognised origin.")


def _origin_of(request: Request) -> str | None:
    """The requesting origin, normalised to `scheme://host[:port]`."""
    header = request.headers.get("origin")
    if header is not None and header != "null":
        return _normalised(header)

    referer = request.headers.get("referer")
    if referer is not None:
        return _normalised(referer)

    return None


def _normalised(value: str) -> str | None:
    """`scheme://host[:port]` from a URL, or `None` if it is not one.

    Rebuilt from the parsed parts rather than string-matched, so that
    `https://arena64.uz/../evil` and `https://arena64.uz.evil.com` cannot
    pass a comparison that a prefix check would let through.
    """
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def verify_trusted_origin(request: Request, settings: SettingsDep) -> None:
    """The dependency form, for the routes that authenticate by cookie.

    A `Depends` rather than a call inside each handler, so the check cannot
    be forgotten by a route added later — it is part of the signature, and
    a handler that needs it declares it the way it declares its services.

    Runs **before** the handler body, so an untrusted request never reaches
    the session store at all.
    """
    enforce_trusted_origin(request, settings.browser_session)


TrustedOriginDep = Annotated[None, Depends(verify_trusted_origin)]


__all__ = [
    "TrustedOriginDep",
    "UntrustedOrigin",
    "enforce_trusted_origin",
    "verify_trusted_origin",
]
