"""The two short-lived cookies "Continue with Google" uses — A64-031.E1.

    arena64_google_state     the binding value from `start`, so only the
                             browser that began a sign-in can finish it
    arena64_google_pending   the ticket for a verified identity waiting
                             for a username or a password

Both `HttpOnly`, both scoped to the Google routes and no wider, and both
gone within minutes — a lifetime that matches the Redis record they point
at, so neither outlives what it names.

## Why `SameSite=Lax` here, whatever the refresh cookie is set to

The callback is a top-level navigation *from accounts.google.com*: a
cross-site request by definition. `Strict` would withhold the binding
cookie from exactly the request that needs it, and every sign-in would
fail as "expired". `Lax` is sent on a cross-site top-level `GET` and on
nothing else cross-site, which is precisely this flow's shape; the two
`POST` routes that read the pending ticket also carry the trusted-origin
check (`browser_csrf.py`).
"""

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Depends, Request, Response

from app.api.deps import SettingsDep

BINDING_COOKIE: Final = "arena64_google_state"
PENDING_COOKIE: Final = "arena64_google_pending"


@dataclass(frozen=True, slots=True)
class ExternalSignInCookies:
    path: str
    secure: bool
    binding_max_age: int
    pending_max_age: int

    def read_binding(self, request: Request) -> str | None:
        return request.cookies.get(BINDING_COOKIE)

    def write_binding(self, response: Response, value: str) -> None:
        self._write(response, BINDING_COOKIE, value, self.binding_max_age)

    def clear_binding(self, response: Response) -> None:
        self._clear(response, BINDING_COOKIE)

    def read_pending(self, request: Request) -> str | None:
        return request.cookies.get(PENDING_COOKIE)

    def write_pending(self, response: Response, value: str) -> None:
        self._write(response, PENDING_COOKIE, value, self.pending_max_age)

    def clear_pending(self, response: Response) -> None:
        self._clear(response, PENDING_COOKIE)

    def _write(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            key=name,
            value=value,
            max_age=max_age,
            path=self.path,
            httponly=True,
            secure=self.secure,
            samesite="lax",
        )

    def _clear(self, response: Response, name: str) -> None:
        # The attributes that wrote it — see `browser_cookie.py` on why a
        # deletion at another path leaves the original behind.
        response.delete_cookie(
            key=name, path=self.path, httponly=True, secure=self.secure, samesite="lax"
        )


def get_external_sign_in_cookies(settings: SettingsDep) -> ExternalSignInCookies:
    browser = settings.browser_session
    google = settings.google_sign_in
    return ExternalSignInCookies(
        path=f"{browser.cookie_path}/google",
        secure=browser.secure_for(settings.environment),
        binding_max_age=google.state_ttl_seconds,
        pending_max_age=google.pending_ttl_seconds,
    )


ExternalSignInCookiesDep = Annotated[ExternalSignInCookies, Depends(get_external_sign_in_cookies)]


__all__ = [
    "BINDING_COOKIE",
    "PENDING_COOKIE",
    "ExternalSignInCookies",
    "ExternalSignInCookiesDep",
    "get_external_sign_in_cookies",
]
