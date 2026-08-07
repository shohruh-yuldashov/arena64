"""The browser's session surface — A64-020.2.

Five endpoints, and **not one new rule**. Every one of them calls the same
application services `/auth/login`, `/auth/refresh` and `/auth/logout`
already call; what differs is where the refresh token travels.

    POST /auth/browser/register     account, then a session, in one call
    POST /auth/browser/login        credentials in, access token out
    POST /auth/browser/refresh      cookie in, rotated cookie + token out
    POST /auth/browser/logout       revoke this device, clear the cookie
    POST /auth/browser/logout-all   revoke every device

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

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.responses import ApiResponse
from app.modules.auth.application.commands import AuthenticateUser, RegisterUser
from app.modules.auth.application.services import IssuedAccessToken
from app.modules.auth.domain.exceptions import SessionNotFound
from app.modules.auth.domain.sessions import RevocationReason, SessionDevice
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
from app.modules.auth.presentation.rate_limits import (
    LOGIN_RATE_LIMIT,
    REFRESH_RATE_LIMIT,
    REGISTER_RATE_LIMIT,
)
from app.modules.auth.presentation.schemas import LoginRequest, RegisterRequest
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
_TOO_MANY_REQUESTS: Responses = error_response(429, "Too many attempts. Try again later.")


def _device_of(request: Request) -> SessionDevice:
    """What the session row records about this browser.

    The same truncations `/auth/login` applies, and for the same reason:
    the header is attacker-controlled and lands in a column.
    """
    user_agent = request.headers.get("user-agent")
    return SessionDevice(
        device_name=user_agent[:120] if user_agent else None,
        user_agent=user_agent[:512] if user_agent else None,
        ip_address=request.client.host if request.client else None,
    )


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
    request: Request,
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

    issued = await sessions.create_session(created.id, device=_device_of(request))
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
    request: Request,
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
    issued = await sessions.create_session(account.id, device=_device_of(request))
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


__all__ = ["browser_auth_router"]
