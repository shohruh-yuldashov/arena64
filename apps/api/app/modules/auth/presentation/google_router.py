""" "Continue with Google" — A64-031.E1, ADR-008.

Six routes under the browser surface, and no new session machinery: a
Google sign-in ends in the same `SessionService.create_session`, the same
refresh cookie and the same access token as `POST /auth/browser/login`.

    GET  /auth/browser/providers          which outside sign-ins exist
    GET  /auth/browser/google/start       -> 302 to Google
    GET  /auth/browser/google/callback    <- Google; -> 302 into the app
    GET  /auth/browser/google/pending     what a verified identity awaits
    POST /auth/browser/google/account     choose a username; signed in
    POST /auth/browser/google/link        enter the password; linked, signed in

## Why `start` and `callback` answer every failure with a redirect

They are browser navigations, not API calls: there is no page to read a
JSON error. Every outcome — cancelled at Google, expired, refused,
rate-limited, not configured — sends the browser back to `/login` with
`?google=<reason>`, which the sign-in page translates. Never a stack trace,
never a token, never the reason in more detail than the page can use.

## What never appears in a URL

The session. A signed-in callback writes the refresh cookie on its
redirect and sends the browser to a plain in-app path; the app then starts
the way any page load does, by refreshing from the cookie. The access
token is never in a query string, and the only values Google puts there —
`code` and `state` — are spent before the response leaves.
"""

import logging
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import RateLimiterDep, RateLimitSettingsDep, SettingsDep
from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.config.settings import Settings
from app.core.exceptions import TooManyRequests
from app.core.responses import ApiResponse
from app.modules.auth.application.services import AwaitingStep
from app.modules.auth.domain.exceptions import (
    AccountLocked,
    AccountRestricted,
    ExternalSignInExpired,
    GoogleSignInRejected,
    InactiveAccount,
)
from app.modules.auth.domain.google_sign_in import PendingPurpose, SignInFailure
from app.modules.auth.presentation.browser_cookie import RefreshCookieDep
from app.modules.auth.presentation.browser_csrf import TrustedOriginDep
from app.modules.auth.presentation.dependencies import (
    AccessTokenServiceDep,
    GoogleSignInServiceDep,
    SessionServiceDep,
)
from app.modules.auth.presentation.device import SessionDeviceDep
from app.modules.auth.presentation.external_sign_in_cookie import ExternalSignInCookiesDep
from app.modules.auth.presentation.rate_limits import (
    GOOGLE_CALLBACK_RATE_LIMIT,
    GOOGLE_START_RATE_LIMIT,
    LOGIN_RATE_LIMIT,
    PROVIDERS_RATE_LIMIT,
    REGISTER_RATE_LIMIT,
)
from app.modules.auth.presentation.schemas import BrowserSession
from app.modules.auth.presentation.schemas.google import (
    GoogleAccountRequest,
    GoogleLinkRequest,
    PendingGoogleSignInRead,
    SignInProviders,
)
from app.modules.notifications.presentation.dependencies import (
    PresenceNotificationServiceDep,
)
from app.modules.users.public import UserRead

logger = logging.getLogger(__name__)

google_auth_router = APIRouter(prefix="/auth/browser", tags=["auth"])

#: Where each pending step is finished — `apps/web` routes.
_STEP_PAGES = {
    PendingPurpose.CREATE_ACCOUNT: "/register/google",
    PendingPurpose.LINK_ACCOUNT: "/connect-google",
}

#: Values `?google=` may carry back to the sign-in page, beyond
#: `SignInFailure`'s own. Each is a sentence on that page, in three
#: languages; anything else would render as nothing.
_UNAVAILABLE = "unavailable"
_RATE_LIMITED = "rate_limited"
_ACCOUNT_STATES: dict[type[Exception], str] = {
    InactiveAccount: "inactive_account",
    AccountLocked: "account_locked",
    AccountRestricted: "account_restricted",
}

#: Bounds on what Google puts in the callback's query. Generous for real
#: values and a ceiling on what an attacker can make this route parse.
_QUERY_VALUE_MAX = 2048

_EXPIRED: Responses = error_response(401, "There is no Google sign-in in progress in this browser.")
_FORBIDDEN: Responses = error_response(
    403, "The account may not sign in, or the request came from an unrecognised origin."
)
_CONFLICT: Responses = error_response(
    409, "The username is taken, or the Google account is already connected."
)
_UNPROCESSABLE: Responses = error_response(422, "A field failed validation.")
_TOO_MANY_REQUESTS: Responses = error_response(429, "Too many attempts. Try again later.")


@google_auth_router.get(
    "/providers",
    dependencies=[Depends(PROVIDERS_RATE_LIMIT)],
    summary="Which outside sign-in methods are offered",
    responses={**_TOO_MANY_REQUESTS},
)
async def sign_in_providers(settings: SettingsDep) -> ApiResponse[SignInProviders]:
    """`google` is `true` exactly when this deployment holds a Google
    client — see `GoogleSignInSettings`. Unauthenticated and cache-safe:
    it says nothing about anybody."""
    return build_response(SignInProviders(google=settings.google_sign_in.is_configured))


@google_auth_router.get(
    "/google/start",
    summary="Begin signing in with Google",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    # FastAPI documents a 422 for any route with query parameters; this one
    # never sends it — every failure is a redirect — but the declaration
    # must carry the platform's error shape like every other.
    responses={**_UNPROCESSABLE},
)
async def google_start(
    request: Request,
    settings: SettingsDep,
    google: GoogleSignInServiceDep,
    cookies: ExternalSignInCookiesDep,
    limiter: RateLimiterDep,
    rate_settings: RateLimitSettingsDep,
    # Unbounded here and bounded by `safe_next_path`: an over-long `next`
    # lands at home rather than on a page of JSON.
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    """Redirects the browser to Google's consent screen.

    Sets the binding cookie on the way out; `state`, `nonce` and the PKCE
    challenge travel in Google's URL. `next` is where to land afterwards —
    an in-app path, checked again on this side (`safe_next_path`).
    """
    probe = Response()
    try:
        await GOOGLE_START_RATE_LIMIT.enforce(
            request, probe, limiter=limiter, settings=rate_settings
        )
    except TooManyRequests:
        return _back_to_sign_in(settings, _RATE_LIMITED)
    if google is None:
        return _back_to_sign_in(settings, _UNAVAILABLE)

    begun = await google.begin(next_path)
    redirect = _redirect(begun.authorization_url)
    cookies.write_binding(redirect, begun.binding)
    return redirect


@google_auth_router.get(
    "/google/callback",
    summary="Google returns here",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={**_UNPROCESSABLE},
)
async def google_callback(
    request: Request,
    settings: SettingsDep,
    google: GoogleSignInServiceDep,
    cookies: ExternalSignInCookiesDep,
    refresh_cookie: RefreshCookieDep,
    sessions: SessionServiceDep,
    presence: PresenceNotificationServiceDep,
    device: SessionDeviceDep,
    limiter: RateLimiterDep,
    rate_settings: RateLimitSettingsDep,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Finishes the round trip and sends the browser into the app.

    Signed in, it lands on `next` holding a refresh cookie. A verified
    identity with no account lands on the username page; one whose address
    an account already uses lands on the password page. Anything else
    lands on `/login?google=<reason>`. The binding cookie is cleared on
    every path — it was good for this one callback.
    """
    redirect = await _complete_callback(
        request,
        settings=settings,
        google=google,
        cookies=cookies,
        refresh_cookie=refresh_cookie,
        sessions=sessions,
        presence=presence,
        device=device,
        limiter=limiter,
        rate_settings=rate_settings,
        state=state,
        code=code,
        error=error,
    )
    cookies.clear_binding(redirect)
    return redirect


async def _complete_callback(
    request: Request,
    *,
    settings: Settings,
    google: GoogleSignInServiceDep,
    cookies: ExternalSignInCookiesDep,
    refresh_cookie: RefreshCookieDep,
    sessions: SessionServiceDep,
    presence: PresenceNotificationServiceDep,
    device: SessionDeviceDep,
    limiter: RateLimiterDep,
    rate_settings: RateLimitSettingsDep,
    state: str | None,
    code: str | None,
    error: str | None,
) -> RedirectResponse:
    try:
        await GOOGLE_CALLBACK_RATE_LIMIT.enforce(
            request, Response(), limiter=limiter, settings=rate_settings
        )
    except TooManyRequests:
        return _back_to_sign_in(settings, _RATE_LIMITED)
    if google is None:
        return _back_to_sign_in(settings, _UNAVAILABLE)
    if any(len(value) > _QUERY_VALUE_MAX for value in (state, code, error) if value):
        # Checked here rather than by `Query(max_length=...)`, which would
        # answer a browser navigation with a 422 body.
        return _back_to_sign_in(settings, SignInFailure.FAILED.value)
    if error is not None:
        # RFC 6749 §4.1.2.1. `access_denied` is the person pressing
        # "Cancel"; anything else is Google declining, which they cannot
        # fix by trying differently.
        cancelled = error == "access_denied"
        failure = SignInFailure.CANCELLED if cancelled else SignInFailure.FAILED
        return _back_to_sign_in(settings, failure.value)
    if not state or not code:
        return _back_to_sign_in(settings, SignInFailure.FAILED.value)

    try:
        outcome = await google.complete(
            state=state, code=code, binding=cookies.read_binding(request)
        )
    except GoogleSignInRejected as rejected:
        logger.info("google_sign_in_rejected", extra={"failure": rejected.failure.value})
        return _back_to_sign_in(settings, rejected.failure.value)
    except (InactiveAccount, AccountLocked, AccountRestricted) as refused:
        return _back_to_sign_in(settings, _ACCOUNT_STATES[type(refused)])

    if isinstance(outcome, AwaitingStep):
        redirect = _redirect(_app_url(settings, _STEP_PAGES[outcome.purpose]))
        cookies.write_pending(redirect, outcome.ticket)
        return redirect

    issued = await sessions.create_session(outcome.account.id, device=device)
    await presence.record_online(outcome.account.id, session_id=issued.session.id)
    redirect = _redirect(_app_url(settings, outcome.next_path))
    refresh_cookie.write(redirect, issued.refresh_token)
    logger.info(
        "google_login_completed",
        extra={"user_id": str(outcome.account.id), "session_id": str(issued.session.id)},
    )
    return redirect


@google_auth_router.get(
    "/google/pending",
    summary="What this browser's Google sign-in is waiting for",
    responses={**_EXPIRED},
)
async def google_pending(
    request: Request,
    google: GoogleSignInServiceDep,
    cookies: ExternalSignInCookiesDep,
) -> ApiResponse[PendingGoogleSignInRead]:
    """`401 external_sign_in_expired` when there is nothing — the page
    offers "Continue with Google" again."""
    if google is None:
        raise ExternalSignInExpired("There is no Google sign-in in progress.")
    pending = await google.pending(cookies.read_pending(request))
    return build_response(PendingGoogleSignInRead.of(pending))


@google_auth_router.post(
    "/google/account",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(REGISTER_RATE_LIMIT)],
    summary="Create the account a Google sign-in is waiting for",
    responses={**_EXPIRED, **_FORBIDDEN, **_CONFLICT, **_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def google_create_account(
    payload: GoogleAccountRequest,
    request: Request,
    response: Response,
    _origin: TrustedOriginDep,
    google: GoogleSignInServiceDep,
    cookies: ExternalSignInCookiesDep,
    refresh_cookie: RefreshCookieDep,
    sessions: SessionServiceDep,
    access_tokens: AccessTokenServiceDep,
    presence: PresenceNotificationServiceDep,
    device: SessionDeviceDep,
) -> ApiResponse[BrowserSession]:
    """Creates the account — verified, linked, through `users`' own
    creation path — and signs the browser in.

    Counted by the same per-IP rule as `POST /auth/browser/register`, so
    Google is not a second, unthrottled way to mint accounts. A taken
    username is `409 username_already_exists` and leaves the sign-in
    pending for another try.
    """
    if google is None:
        raise ExternalSignInExpired("There is no Google sign-in in progress.")
    created = await google.create_account(
        cookies.read_pending(request),
        username=payload.username,
        preferred_language=payload.preferred_language.value,
        timezone=payload.timezone,
    )
    session = await _start_session(
        created,
        response=response,
        refresh_cookie=refresh_cookie,
        sessions=sessions,
        access_tokens=access_tokens,
        presence=presence,
        device=device,
    )
    cookies.clear_pending(response)
    return build_response(session)


@google_auth_router.post(
    "/google/link",
    dependencies=[Depends(LOGIN_RATE_LIMIT)],
    summary="Connect Google to the account that already uses its address",
    responses={**_EXPIRED, **_FORBIDDEN, **_CONFLICT, **_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def google_link(
    payload: GoogleLinkRequest,
    request: Request,
    response: Response,
    _origin: TrustedOriginDep,
    google: GoogleSignInServiceDep,
    cookies: ExternalSignInCookiesDep,
    refresh_cookie: RefreshCookieDep,
    sessions: SessionServiceDep,
    access_tokens: AccessTokenServiceDep,
    presence: PresenceNotificationServiceDep,
    device: SessionDeviceDep,
) -> ApiResponse[BrowserSession]:
    """Verifies the account's password, links Google, and signs in.

    The explicit step ADR-008 requires: an address match alone never
    links. A wrong password is the same `401 invalid_credentials` a sign-in
    gives, under the same per-IP limit, and leaves the sign-in pending.
    """
    if google is None:
        raise ExternalSignInExpired("There is no Google sign-in in progress.")
    account = await google.link_account(cookies.read_pending(request), payload.password)
    session = await _start_session(
        account,
        response=response,
        refresh_cookie=refresh_cookie,
        sessions=sessions,
        access_tokens=access_tokens,
        presence=presence,
        device=device,
    )
    cookies.clear_pending(response)
    return build_response(session)


async def _start_session(
    account: UserRead,
    *,
    response: Response,
    refresh_cookie: RefreshCookieDep,
    sessions: SessionServiceDep,
    access_tokens: AccessTokenServiceDep,
    presence: PresenceNotificationServiceDep,
    device: SessionDeviceDep,
) -> BrowserSession:
    """What `POST /auth/browser/login` does after authentication, in the
    same order: session, access token, presence, cookie."""
    issued = await sessions.create_session(account.id, device=device)
    access = access_tokens.create_access_token(account)
    await presence.record_online(account.id, session_id=issued.session.id)
    refresh_cookie.write(response, issued.refresh_token)
    logger.info(
        "google_login_completed",
        extra={"user_id": str(account.id), "session_id": str(issued.session.id)},
    )
    return BrowserSession.of(access, account)


def _app_url(settings: Settings, path: str, **query: str) -> str:
    """`PUBLIC_APP_URL` + an in-app path. The origin is configuration, never
    input, so no redirect from here can leave the platform."""
    suffix = f"?{urlencode(query)}" if query else ""
    return f"{settings.app.public_url}{path}{suffix}"


def _back_to_sign_in(settings: Settings, reason: str) -> RedirectResponse:
    return _redirect(_app_url(settings, "/login", google=reason))


def _redirect(url: str) -> RedirectResponse:
    redirect = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    # A response that sets or spends a credential is never cached, and the
    # callback's own URL — which held a code — is never sent onwards.
    redirect.headers["Cache-Control"] = "no-store"
    redirect.headers["Referrer-Policy"] = "no-referrer"
    return redirect


__all__ = ["google_auth_router"]
