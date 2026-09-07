"""The browser's session surface — A64-020.2.

Seven endpoints, and **not one new rule**. Every one of them calls the same
application services `/auth/login`, `/auth/refresh` and `/auth/logout`
already call; what differs is where the refresh token travels.

    POST /auth/browser/register     account, then a session, in one call
    POST /auth/browser/login        credentials in, access token out
    POST /auth/browser/refresh      cookie in, rotated cookie + token out
    POST /auth/browser/logout       revoke this device, clear the cookie
    POST /auth/browser/logout-all   revoke every device
    GET  /auth/browser/sessions     the devices this account is signed in on
    DELETE /auth/browser/sessions/{id}  revoke one of them

## Why a second surface rather than a flag on the first

`/auth/login` returns the refresh token in the body, which is correct for
a native client: it holds the credential itself and can put it in a
keychain. A browser cannot. Every place a page can store a string is
readable by any script that reaches that page, so the only safe home for a
thirty-day credential is a cookie the page cannot read.

Those are genuinely different contracts, not one contract with a switch. A
`?cookie=true` parameter would mean one endpoint whose response body
changes shape by query string — undocumentable in OpenAPI, and a client
that got the flag wrong would silently receive a credential it could not
store safely. **The JSON endpoints are unchanged**, and remain what
everything that is not a browser uses.

## What the browser never receives

The refresh token, in any response body, on any of these endpoints. That is
the whole point: `_issue_session` writes it to the cookie and returns only
the access token and a user summary. A refresh token in a body here would
be readable by script, and the cookie would be decoration.

## Register signs the user in

Unlike `/auth/register`, which deliberately issues nothing. The asymmetry
is a product decision about a *browser*: a person who has just filled in a
sign-up form and is looking at the app should be in it, not at a login
form asking for the password they typed ten seconds ago. Verification is
unchanged — the account is unverified either way, and whatever gating the
platform later applies to unverified accounts applies to this session too.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.modules.auth.application.commands import AuthenticateUser, RegisterUser
from app.modules.auth.application.services import IssuedAccessToken
from app.modules.auth.domain.exceptions import SessionNotFound
from app.modules.auth.domain.sessions import RevocationReason
from app.modules.auth.presentation.browser_cookie import RefreshCookieDep
from app.modules.auth.presentation.browser_csrf import TrustedOriginDep
from app.modules.auth.presentation.dependencies import (
    AccessTokenServiceDep,
    AuthenticationServiceDep,
    CurrentUser,
    EmailVerificationServiceDep,
    RegistrationServiceDep,
    SessionServiceDep,
    UserProfileReaderDep,
)
from app.modules.auth.presentation.device import SessionDeviceDep
from app.modules.auth.presentation.rate_limits import (
    LOGIN_RATE_LIMIT,
    REFRESH_RATE_LIMIT,
    REGISTER_RATE_LIMIT,
)
from app.modules.auth.presentation.schemas import LoginRequest, RegisterRequest, SessionRead
from app.modules.auth.presentation.schemas.browser import BrowserSession
from app.modules.notifications.presentation.dependencies import (
    PresenceNotificationServiceDep,
)
from app.modules.users.public import UserRead

logger = logging.getLogger(__name__)

browser_auth_router = APIRouter(prefix="/auth/browser", tags=["auth"])

_UNAUTHORIZED: Responses = error_response(
    401, "The session cookie was missing, expired or already rotated."
)
_FORBIDDEN: Responses = error_response(
    403, "The account may not sign in, or the request came from an unrecognised origin."
)
_CONFLICT: Responses = error_response(409, "The username or email address is taken.")
_UNPROCESSABLE: Responses = error_response(422, "A field failed validation.")
_NOT_FOUND: Responses = error_response(404, "No such device on this account.")
_TOO_MANY_REQUESTS: Responses = error_response(429, "Too many attempts. Try again later.")


def _session_of(access: IssuedAccessToken, user: UserRead) -> BrowserSession:
    return BrowserSession.of(access, user)


@browser_auth_router.post(
    "/register",
    dependencies=[Depends(REGISTER_RATE_LIMIT)],
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and start a browser session",
    responses={**_CONFLICT, **_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def browser_register(
    payload: RegisterRequest,
    device: SessionDeviceDep,
    response: Response,
    cookie: RefreshCookieDep,
    registration: RegistrationServiceDep,
    verification: EmailVerificationServiceDep,
    sessions: SessionServiceDep,
    access_tokens: AccessTokenServiceDep,
    presence: PresenceNotificationServiceDep,
) -> ApiResponse[BrowserSession]:
    """Registers, sends a six-digit code, and signs the browser in.

    **A code, not a link** — A64-021.5H. The session exists either way and
    the account is unverified either way; what changed is that the person
    carries six digits from their inbox to the page they are already on,
    rather than moving a session to wherever their mail is.

    Signing in before verification is unchanged and is deliberate: the
    frontend needs an authenticated call to submit the code, and the
    verified-email policy (`VerifiedUser`) is what stops that session doing
    anything else.

    A delivery failure still never fails the request. The account exists,
    the challenge is committed, and the person can ask for another code —
    turning a transient vendor outage into a failed registration would be
    the worse trade.
    """
    created = await registration.register(
        RegisterUser(
            username=payload.username,
            email=payload.email,
            password=payload.password,
            preferred_language=payload.preferred_language.value,
            timezone=payload.timezone,
            display_name=payload.display_name,
        )
    )
    await verification.send_verification_code(created)

    issued = await sessions.create_session(created.id, device=device)
    access = access_tokens.create_access_token(created)
    await presence.record_online(created.id, session_id=issued.session.id)

    cookie.write(response, issued.refresh_token)
    logger.info(
        "browser_register_completed",
        extra={"user_id": str(created.id), "session_id": str(issued.session.id)},
    )
    return build_response(_session_of(access, created))


@browser_auth_router.post(
    "/login",
    dependencies=[Depends(LOGIN_RATE_LIMIT)],
    summary="Sign in and start a browser session",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def browser_login(
    payload: LoginRequest,
    device: SessionDeviceDep,
    response: Response,
    cookie: RefreshCookieDep,
    authentication: AuthenticationServiceDep,
    access_tokens: AccessTokenServiceDep,
    sessions: SessionServiceDep,
    presence: PresenceNotificationServiceDep,
) -> ApiResponse[BrowserSession]:
    """Verifies credentials, then puts the refresh token in the cookie.

    Identical to `POST /auth/login` in everything that decides the outcome
    — the same service, the same rate limit, the same
    `401 invalid_credentials` whether the address is unknown or the
    password is wrong, in the same elapsed time. The one difference is that
    the refresh token is written to a cookie instead of the body.
    """
    account = await authentication.authenticate(
        AuthenticateUser(email=payload.email, password=payload.password)
    )
    issued = await sessions.create_session(account.id, device=device)
    access = access_tokens.create_access_token(account)
    await presence.record_online(account.id, session_id=issued.session.id)

    cookie.write(response, issued.refresh_token)
    logger.info(
        "browser_login_completed",
        extra={"user_id": str(account.id), "session_id": str(issued.session.id)},
    )
    return build_response(_session_of(access, account))


@browser_auth_router.post(
    "/refresh",
    dependencies=[Depends(REFRESH_RATE_LIMIT)],
    summary="Rotate the browser session",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_TOO_MANY_REQUESTS},
)
async def browser_refresh(
    request: Request,
    response: Response,
    cookie: RefreshCookieDep,
    _origin: TrustedOriginDep,
    sessions: SessionServiceDep,
    access_tokens: AccessTokenServiceDep,
    profiles: UserProfileReaderDep,
    presence: PresenceNotificationServiceDep,
) -> ApiResponse[BrowserSession]:
    """Exchanges the cookie for a new access token and a rotated cookie.

    **No request body.** The credential is the cookie, and an endpoint that
    also accepted one in the body would be an endpoint a page could call
    with a token it had somehow obtained — reintroducing exactly what the
    cookie exists to prevent.

    Rotation is `SessionService`'s, unchanged: the presented token stops
    working immediately, and presenting an already-rotated one revokes the
    whole chain. That last rule is why the frontend's refresh is
    single-flight — two concurrent refreshes would present the same token
    twice and sign the user out.

    A missing cookie is `401`, the same answer as an invalid one. The two
    are indistinguishable to a caller whose next step is identical: sign in.
    """
    presented = cookie.read(request)
    if presented is None:
        # Cleared rather than merely refused: a browser holding a cookie
        # this server will not accept should stop sending it.
        cookie.clear(response)
        raise SessionNotFound("There is no active browser session.")

    rotated = await sessions.rotate_refresh_token(presented)
    account = await profiles.get_profile(rotated.session.user_id)
    access = access_tokens.create_access_token(account)
    await presence.record_online(rotated.session.user_id, session_id=rotated.session.id)

    cookie.write(response, rotated.refresh_token)
    return build_response(_session_of(access, account))


@browser_auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of this browser",
    responses=_FORBIDDEN,
)
async def browser_logout(
    request: Request,
    cookie: RefreshCookieDep,
    _origin: TrustedOriginDep,
    sessions: SessionServiceDep,
) -> Response:
    """Revokes this browser's session and expires the cookie.

    **Idempotent, and unauthenticated on purpose.** Signing out must
    succeed when the session is already gone, when the cookie is stale, and
    when it was never there — a client that cannot complete a sign-out is a
    client that shows a signed-in user who is not. So a missing or unknown
    cookie is `204`, and the cookie is cleared either way.

    The revocation is best-effort for the same reason: if the token names
    no live session there is nothing to revoke and nothing has gone wrong.
    """
    presented = cookie.read(request)
    if presented is not None:
        await sessions.revoke_by_refresh_token(presented, reason=RevocationReason.PLAYER)

    empty = Response(status_code=status.HTTP_204_NO_CONTENT)
    cookie.clear(empty)
    return empty


@browser_auth_router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of every device",
    responses={**_UNAUTHORIZED, **_FORBIDDEN},
)
async def browser_logout_all(
    user: CurrentUser,
    cookie: RefreshCookieDep,
    _origin: TrustedOriginDep,
    sessions: SessionServiceDep,
    presence: PresenceNotificationServiceDep,
) -> Response:
    """Revokes every session for the account, and clears this cookie.

    Authenticated by the **access token**, not the cookie — the same choice
    `POST /auth/logout-all` makes, and for the same reason: this acts on
    the account rather than on one device, so the credential that names an
    account is the right one. A player whose laptop was taken can sign out
    everywhere from their phone.

    The cookie is still cleared here, because this browser's session is one
    of the ones just revoked and leaving it would mean the next refresh
    presents a revoked token — a `401` where a clean anonymous state was
    the honest outcome.
    """
    revoked = await sessions.revoke_all_sessions(user.id, reason=RevocationReason.PLAYER)
    await presence.record_offline(user.id)

    logger.info(
        "browser_logout_all_completed",
        extra={"user_id": str(user.id), "sessions_revoked": revoked},
    )

    empty = Response(status_code=status.HTTP_204_NO_CONTENT)
    cookie.clear(empty)
    return empty


# --- The device list — A64-030.5C, SE-2 --------------------------------------
#
# ## Why these two live here and not under `/auth/sessions`
#
# The list has to be able to say *which row is this browser*, and nothing
# on the JSON surface can. An access token names an **account**: it carries
# `sub` and `jti` and deliberately no `sid`, so two devices of the same
# player present indistinguishable credentials. The refresh token is the
# only thing that names one device, and in a browser it is the cookie —
# whose path is `BROWSER_SESSION_COOKIE_PATH`, `/api/v1/auth/browser`.
#
# A `GET /api/v1/auth/sessions` would therefore be served a request the
# cookie was never sent with, and `is_current` would be `false` on every
# row. The three ways to avoid that were:
#
#   widen the cookie path       sends a thirty-day credential to every
#                               endpoint under `/auth`, to label a row
#   add a `sid`/family claim    a token format change, and every access
#                               token already in circulation lacks it
#   put the list where the      one route decorator
#     cookie already goes
#
# The third is the smallest and gives up nothing: this list is a browser
# screen, and a native client — which holds its refresh token itself — can
# be given the same list under `/auth` on the day one exists, from the same
# service methods.
#
# ## What the cookie is and is not used for here
#
# **Not authentication.** Both routes authenticate with `CurrentUser`, the
# access token, exactly as `logout-all` does. The cookie is read on the
# `GET` for one purpose — deciding which row is marked — and a missing or
# stale cookie costs the marker and nothing else.


@browser_auth_router.get(
    "/sessions",
    summary="The devices this account is signed in on",
    response_description="One entry per device, newest sign-in first.",
    responses={**_UNAUTHORIZED},
)
async def browser_sessions(
    user: CurrentUser,
    request: Request,
    cookie: RefreshCookieDep,
    sessions: SessionServiceDep,
) -> ApiResponse[list[SessionRead]]:
    """Lists this account's live devices — SE-2.

    One entry per **device**, not per session row: rotation replaces the
    row four times an hour and the family is what survives it. The
    identifier in each entry is what `DELETE` below takes.

    Scoped by the access token and by nothing the caller sends. There is no
    user id in the path or the query, so this cannot return somebody
    else's list even if a client tried.

    No refresh token, no hash and no address leaves here — see
    `schemas/sessions.py` on which of those is absent by construction and
    which is a product decision.
    """
    presented = cookie.read(request)
    current_family = (
        await sessions.device_for_refresh_token(presented) if presented is not None else None
    )
    devices = await sessions.list_user_devices(user.id)

    return build_response(
        [SessionRead.of(device, current_family=current_family) for device in devices]
    )


@browser_auth_router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign one device out",
    # `_UNPROCESSABLE` because the identifier is a path parameter:
    # FastAPI declares a `422` for it whether or not this says so, and
    # its default schema is FastAPI's own `HTTPValidationError` rather
    # than the platform envelope every other failure on this module
    # uses. Naming it here replaces that with `ErrorResponse`.
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_NOT_FOUND, **_UNPROCESSABLE},
)
async def browser_revoke_session(
    session_id: UUID,
    user: CurrentUser,
    _origin: TrustedOriginDep,
    sessions: SessionServiceDep,
) -> Response:
    """Revokes one device's whole rotation chain.

    `204` whether or not this call was the one that revoked it: a device
    already signed out is the state the caller asked for, and a retry
    after a dropped response must not be an error.

    `404` when the identifier is not one of this account's devices —
    `SessionService.revoke_device` makes that check, and it is the only
    thing standing between this route and a cross-user revoke. It is
    deliberately not `401`: this browser's own credentials are fine, and a
    `401` would make the client's unauthorised interceptor sign the whole
    account out over one mistyped identifier.

    ## Revoking the device you are using

    Permitted, and the same thing happens as when another device revokes
    you: the refresh cookie stops working, so the browser is signed out at
    its next refresh rather than immediately — the access token it already
    holds stays valid for its fifteen minutes.

    That delay is why the UI does not offer it. `POST /auth/browser/logout`
    is what "sign out of this device" means: it revokes *and* clears the
    cookie, so the browser is anonymous straight away. Two routes, no
    conflicting semantics — this one acts on a device in a list, that one
    acts on the browser making the request.

    Carries the trusted-origin check every other state-changing browser
    route carries. A `DELETE` triggered from another site would otherwise
    be able to sign a signed-in player out of their own devices.
    """
    await sessions.revoke_device(user.id, session_id, reason=RevocationReason.PLAYER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["browser_auth_router"]
