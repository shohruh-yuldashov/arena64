"""The refresh cookie, as one resolved value — A64-020.2.

`BrowserSessionSettings` says what the deployment wants; this says what
this request will actually write, with the environment already folded in
and the lifetime already converted. One value object rather than five
arguments threaded through four handlers, and one place where "the cookie
we write" and "the cookie we delete" are the same thing.

**That last property is the reason this exists.** A browser matches a
deletion against the cookie's name *and* path; a `delete_cookie` whose path
differs from the `set_cookie` that wrote it silently creates a second,
already-expired cookie and leaves the live one in the jar. A sign-out that
appears to work and leaves a thirty-day credential behind is exactly the
failure this shape makes unrepresentable.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Request, Response

from app.api.deps import SettingsDep


@dataclass(frozen=True, slots=True)
class RefreshCookie:
    """Where this deployment keeps the browser's refresh token."""

    name: str
    path: str
    secure: bool
    same_site: Literal["lax", "strict", "none"]
    max_age_seconds: int

    def read(self, request: Request) -> str | None:
        """The presented refresh token, from the cookie and nowhere else.

        Never a body field, never a header, never a query parameter. If the
        page could supply it, the page could read it — and every reason for
        the cookie disappears.
        """
        return request.cookies.get(self.name)

    def write(self, response: Response, refresh_token: str) -> None:
        """Puts the rotated token where script cannot reach it.

        `httponly` is the security property; the rest bounds where and when
        the browser sends it. `max_age` mirrors the session's own absolute
        expiry so the cookie and the row stop being useful together — a
        cookie outliving its session is a request that always `401`s, and
        one that expires first signs out a live session.
        """
        response.set_cookie(
            key=self.name,
            value=refresh_token,
            max_age=self.max_age_seconds,
            path=self.path,
            httponly=True,
            secure=self.secure,
            samesite=self.same_site,
        )

    def clear(self, response: Response) -> None:
        """Expires it, with the attributes that wrote it. See the module
        docstring on why the path has to match."""
        response.delete_cookie(
            key=self.name,
            path=self.path,
            httponly=True,
            secure=self.secure,
            samesite=self.same_site,
        )


def get_refresh_cookie(settings: SettingsDep) -> RefreshCookie:
    """The cookie policy for this process.

    A dependency rather than a module-level constant, so a test can
    override it — and so `secure` is resolved from the environment in one
    place instead of at each call site.
    """
    browser = settings.browser_session
    return RefreshCookie(
        name=browser.cookie_name,
        path=browser.cookie_path,
        secure=browser.secure_for(settings.environment),
        same_site=browser.same_site,
        # Days to seconds, from the session's own absolute window.
        max_age_seconds=settings.session.refresh_token_ttl_days * 24 * 60 * 60,
    )


RefreshCookieDep = Annotated[RefreshCookie, Depends(get_refresh_cookie)]


__all__ = ["RefreshCookie", "RefreshCookieDep", "get_refresh_cookie"]
