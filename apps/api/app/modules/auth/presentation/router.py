"""HTTP routes for `auth` — the authentication API.

Ten endpoints, and **no business logic in any of them**. Every handler
below does the same three things: translate a wire schema into a command,
call one or two existing services, translate the result into a wire
schema. Validation is the schemas' (which reuse the domain validators);
rules are the services'; transactions are theirs too. A handler that
decided anything would be a fourth copy of a rule that already has an
owner.

## Errors need no handling here

Every failure is a typed exception on the platform hierarchy, and
`app/api/exception_handlers.py` maps them by MRO walk. There is not one
`try`/`except` in this file, and that is the design working:

    WeakPassword            -> 422  weak_password
    InvalidUsername         -> 422  invalid_username
    InvalidEmail            -> 422  invalid_email
    UsernameAlreadyExists   -> 409  username_already_exists
    EmailAlreadyExists      -> 409  email_already_exists
    InvalidCredentials      -> 401  invalid_credentials
    InactiveAccount         -> 403  inactive_account
    AccountLocked           -> 403  account_locked
    MissingToken            -> 401  authentication_required
    InvalidToken            -> 401  invalid_token
    ExpiredToken            -> 401  expired_token
    InvalidRefreshToken     -> 401  invalid_session
    ExpiredRefreshToken     -> 401  session_expired
    SessionNotFound         -> 401  invalid_session
    RevokedSession          -> 401  invalid_session
    InvalidVerificationToken-> 422  invalid_verification_token
    InvalidResetToken       -> 422  invalid_reset_token
    TooManyRequests         -> 429  rate_limited

## Rate limiting (A64-011.8)

Six of the ten endpoints carry a `RateLimit` guard, declared in
`rate_limits.py` and attached here as a route dependency. The limits
themselves are not repeated in this file — one place for a number, and it
is `RateLimitSettings`, which is what an operator edits during an
incident.

The guards run **before** Pydantic validates the body, because FastAPI
resolves route dependencies first. That is deliberate: a request with a
malformed body still consumes its per-IP allowance, so "send garbage
quickly" is not a cheaper way to probe an endpoint than sending something
valid.

The four endpoints without a guard are `POST /auth/logout`,
`POST /auth/logout-all`, `GET /auth/me` and `POST /auth/email/verify`.
The first three require a credential the caller must already hold, so
abusing them requires an account and is bounded by whatever produced the
credential; A64-011.8's endpoint list does not include them. The fourth is
listed nowhere either and is guarded by its token's 256 bits rather than
by a counter — though it is a reasonable thing to add, and the
recommendations say so.

`SessionNotFound` is a 401 rather than a 404 on purpose — see its
docstring: a 404 would confirm the endpoint looked something up and did
not find it, which over a session table is a membership oracle.

## The two credentials, and why they differ

`POST /auth/login` and `POST /auth/refresh` return an **access token** —
a stateless 15-minute JWT — and a **refresh token** — an opaque 30-day
row. They are different kinds of thing for a reason database.md §14.3
sets out: the short one is cheap to verify and impossible to revoke, the
long one is revocable precisely because it is stored. Every endpoint here
that takes a credential takes exactly one of them, never either.

## What A64-011.5 deliberately does not do

No cookies. The refresh token is returned in the body, which makes this
API usable from a native client and a browser alike; the browser's
`HttpOnly` cookie is a *client-side* decision the SPA makes when it
stores what it receives. Adding `Set-Cookie` here would make the endpoint
browser-specific and would need CSRF machinery this task does not
include — see the recommendations.

No `GET /auth/sessions`. Listing devices is SE-2's, and the service
method exists (`SessionService.list_user_sessions`), but the task's
endpoint list does not include it and a listing needs a response schema
whose shape belongs with its own task.
"""

import logging

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.openapi import Responses, error_response
from app.api.responses import build_response
from app.core.constants import API_PREFIX, API_V1_PREFIX
from app.core.responses import ApiResponse
from app.modules.auth.application.commands import AuthenticateUser, RegisterUser
from app.modules.auth.domain.sessions import RevocationReason, SessionDevice
from app.modules.auth.presentation.dependencies import (
    AccessTokenServiceDep,
    AuthenticationServiceDep,
    CurrentUser,
    EmailVerificationServiceDep,
    PasswordResetServiceDep,
    RegistrationServiceDep,
    SessionServiceDep,
    UserProfileReaderDep,
    WebSocketTicketServiceDep,
)
from app.modules.auth.presentation.rate_limits import (
    FORGOT_PASSWORD_RATE_LIMIT,
    LOGIN_RATE_LIMIT,
    REFRESH_RATE_LIMIT,
    REGISTER_RATE_LIMIT,
    RESEND_CODE_RATE_LIMIT,
    RESEND_VERIFICATION_RATE_LIMIT,
    RESET_PASSWORD_RATE_LIMIT,
)
from app.modules.auth.presentation.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TokenPair,
    VerificationAccepted,
    VerifyCodeRequest,
    VerifyEmailRequest,
    WebSocketTicketRead,
)
from app.modules.notifications.presentation.dependencies import (
    PresenceNotificationServiceDep,
)
from app.modules.users.public import UserRead

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/auth", tags=["auth"])


#: Reused across every endpoint that can 401, so the documented error
#: shape is declared once. `ErrorResponse` is the platform's only error
#: body (`app/api/exception_handlers.py`), so naming it here makes the
#: generated docs show the real shape rather than FastAPI's default
#: `{"detail": ...}`, which this platform never returns.
_UNAUTHORIZED: Responses = error_response(
    401,
    "The credential was missing, malformed, expired or revoked.",
)
_FORBIDDEN: Responses = error_response(
    403,
    "Credentials were correct but the account may not sign in.",
)
_CONFLICT: Responses = error_response(
    409,
    "The username or email address is already registered.",
)
_UNPROCESSABLE: Responses = error_response(
    422,
    "A field failed validation. `code` names which one.",
)
#: A64-011.9. `GET /auth/me` documented `200` and `401` while its own
#: docstring described a `404` — the narrow window in which an account is
#: deleted while a valid token is still in flight. Prose a client cannot
#: read is not documentation, and a 404 nobody declared is one a generated
#: client has no branch for.
_NOT_FOUND: Responses = error_response(
    404,
    "The account no longer exists — deleted while a valid token was in flight.",
)
#: A64-011.8. Every endpoint below that carries a `RateLimit` guard
#: documents this, because a 429 a client has not been told to expect is
#: one it will retry immediately and in a loop.
#:
#: The description names the headers rather than the *rule*: a caller needs
#: to know when to come back, and deliberately does not learn which
#: dimension refused them — see `TooManyRequests`.
_TOO_MANY_REQUESTS: Responses = error_response(
    429,
    (
        "A rate limit was exceeded. `Retry-After` gives the number of seconds to "
        "wait; `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` "
        "describe the limit that bound. The same three `X-RateLimit-*` headers are "
        "returned on successful responses, so a client can pace itself before "
        "being refused."
    ),
)


def _device_of(request: Request) -> SessionDevice:
    """Describes the caller's device for the session list — SE-2.

    Read from the request here rather than in `SessionService`, because a
    service that knew what a `User-Agent` header was could only be called
    over HTTP — and AD-09's gateway and a future mobile client are both
    callers that are not.

    `device_name` is the raw user agent truncated, not a parsed "Chrome on
    macOS". Parsing user agents needs a dependency with a monthly update
    cadence, and a wrong label is worse than a plain one: SE-2's purpose
    is for a player to *recognise* their devices, and a confidently wrong
    "Firefox on Windows" defeats that more thoroughly than a raw string.
    Parsing belongs with the task that adds the device list UI.

    `request.client` is `None` behind some ASGI setups and in `TestClient`
    without a transport, so the address is optional rather than assumed.
    """
    user_agent = request.headers.get("user-agent")
    return SessionDevice(
        device_name=user_agent[:120] if user_agent else None,
        user_agent=user_agent[:512] if user_agent else None,
        ip_address=request.client.host if request.client else None,
    )


@auth_router.post(
    "/register",
    dependencies=[Depends(REGISTER_RATE_LIMIT)],
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    response_description="The newly created account.",
    responses={**_CONFLICT, **_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def register(
    payload: RegisterRequest,
    service: RegistrationServiceDep,
    verification: EmailVerificationServiceDep,
    response: Response,
) -> ApiResponse[UserRead]:
    """Registers a new account and returns it.

    Returns `201` with a `Location` header pointing at the canonical
    resource — the created account is readable at `GET /api/v1/users/{id}`,
    and saying so is what `201` is for.

    **No tokens are issued.** Registration proves you can fill in a form,
    not that you own the address; signing in is a separate call. Issuing a
    session here would also mean an unverified account holding a 30-day
    credential before A64-011.6 has had a chance to verify anything.

    A six-digit verification code is sent to the address (A64-021.5H).
    Delivery failure does not fail the request — the account exists, the
    challenge is committed, and the person can ask for another code.

    The body carries no `password_hash`: `UserRead` has no such field, so
    that is a property of the type rather than of remembering to exclude
    it. `is_verified` is `false` until the link is redeemed.
    """
    created = await service.register(
        RegisterUser(
            username=payload.username,
            email=payload.email,
            password=payload.password,
            preferred_language=payload.preferred_language.value,
            timezone=payload.timezone,
            display_name=payload.display_name,
        )
    )

    # A64-021.5H: the first verification code. `send_verification_code`
    # never raises for a delivery failure — see `EmailVerificationService`
    # — so a mail provider being briefly unreachable cannot turn a
    # successful registration into a 500. The account exists either way and
    # the person can ask for a new code.
    await verification.send_verification_code(created)

    # Both prefixes: `API_V1_PREFIX` alone is "/v1", and the mount point
    # adds "/api" on top of it (`app_factory`). Composing them here is
    # what makes the header a URL a client can actually follow rather
    # than a path that 404s.
    response.headers["Location"] = f"{API_PREFIX}{API_V1_PREFIX}/users/{created.id}"
    return build_response(created)


@auth_router.post(
    "/login",
    dependencies=[Depends(LOGIN_RATE_LIMIT)],
    summary="Sign in and receive a token pair",
    response_description="An access token and a refresh token.",
    responses={**_UNAUTHORIZED, **_FORBIDDEN, **_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def login(
    payload: LoginRequest,
    request: Request,
    authentication: AuthenticationServiceDep,
    access_tokens: AccessTokenServiceDep,
    sessions: SessionServiceDep,
    presence: PresenceNotificationServiceDep,
) -> ApiResponse[TokenPair]:
    """Verifies credentials and starts a session.

    `200`, not `201`: a session row is created, but the thing the caller
    asked for is a credential, not a resource with a URL.

    Three services, in an order that matters: identity is proven first,
    and only then is anything issued. `AuthenticationService` raises
    before either token service is reached, so a failed sign-in creates no
    session row and mints no JWT.

    A failed sign-in returns `401 invalid_credentials` whether the address
    is unknown or the password is wrong — identically, and in the same
    elapsed time. `403` means the credentials were right but the account
    is deactivated or temporarily locked.

    Each sign-in starts an independent session, so signing in on a phone
    does not disturb a laptop.

    **Marks the player online** (A64-013.6). Presence is best-effort and
    expires on its own timer, so a client that wants to keep showing as
    online must keep refreshing — see `POST /auth/refresh`. Nothing about
    presence can fail this request.
    """
    account = await authentication.authenticate(
        AuthenticateUser(email=payload.email, password=payload.password)
    )
    issued_session = await sessions.create_session(account.id, device=_device_of(request))
    access = access_tokens.create_access_token(account)

    # A64-013.6. **After the session exists**, so a sign-in that failed to
    # issue one records no presence — and never before, because presence
    # asserts a player is here and a failed login has not established that.
    #
    # `PresenceNotificationService` never raises: the presence write cannot
    # (`PresenceRecorder`'s contract) and the event write is guarded, so a
    # Redis or an outbox failure costs an online indicator and not a
    # sign-in.
    #
    # A64-013.7 replaced `PresenceService` here with the coordinator that
    # wraps it, so that a sign-in by a player who was **already** online
    # records presence and emits nothing. The route asks for neither
    # behaviour and gets both — which is the point of the brief's "do NOT
    # call [PresenceService] directly from controllers".
    await presence.record_online(account.id, session_id=issued_session.session.id)

    logger.info(
        "login_completed",
        extra={"user_id": str(account.id), "session_id": str(issued_session.session.id)},
    )
    return build_response(TokenPair.of(access, issued_session.refresh_token))


@auth_router.post(
    "/refresh",
    dependencies=[Depends(REFRESH_RATE_LIMIT)],
    summary="Exchange a refresh token for a new token pair",
    response_description="A new access token and a rotated refresh token.",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def refresh(
    payload: RefreshRequest,
    sessions: SessionServiceDep,
    access_tokens: AccessTokenServiceDep,
    profiles: UserProfileReaderDep,
    presence: PresenceNotificationServiceDep,
) -> ApiResponse[TokenPair]:
    """Rotates the refresh token and issues a fresh access token.

    **The presented refresh token is invalidated.** Every refresh returns
    a new one, and the old one stops working immediately — database.md
    §14.3's rotation-on-every-use. A client must store what it receives;
    retrying with the previous token is indistinguishable from a replay.

    **Presenting an already-rotated token revokes the whole chain.** That
    is the point of rotating: a token used twice was captured, and since
    the platform cannot tell the attacker from the legitimate user, both
    are signed out of that session. Expect `401 invalid_session`, and
    expect the successor to stop working too.

    `401 session_expired` means the session aged out — through the 30-day
    absolute window or the idle window — and the caller must sign in
    again. Every other failure is `401 invalid_session`: unknown token,
    revoked session, malformed value. They are deliberately
    indistinguishable.

    The account is re-read here rather than trusted from the session,
    because the access token must reflect the account as it is *now* — and
    because a deactivated or deleted account must not be able to refresh
    its way to a fresh 15 minutes of access.

    **Marks the player online and restarts their presence window**
    (A64-013.6). This is what keeps a signed-in player visible as online
    without a socket: a client that stops refreshing stops being online one
    `PRESENCE_TTL_SECONDS` later, with nothing having to observe that it
    left. A client that wants to stay online should refresh well inside
    that window.
    """
    rotated = await sessions.rotate_refresh_token(payload.refresh_token)

    # Raises `UserNotFound` (404) if the account is gone. That cannot
    # normally happen — the session's foreign key cascades on delete — but
    # a 404 here is the honest answer rather than a 500.
    account = await profiles.get_profile(rotated.session.user_id)
    access = access_tokens.create_access_token(account)

    # A64-013.6. A client exchanging a refresh token is a client that is
    # still there, so this is what keeps a signed-in player online: the
    # presence record's TTL restarts on every refresh, and a player who
    # closes the tab stops refreshing and goes quiet on its own.
    #
    # A64-013.7: the refresh is the case transition detection exists for. It
    # runs on a timer for as long as a player is signed in, and every one of
    # those would otherwise be a fan-out to every friend about a state that
    # did not change.
    await presence.record_online(rotated.session.user_id, session_id=rotated.session.id)

    logger.info(
        "refresh_completed",
        extra={
            "user_id": str(rotated.session.user_id),
            "session_id": str(rotated.session.id),
        },
    )
    return build_response(TokenPair.of(access, rotated.refresh_token))


@auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of the current device",
    response_description="The session was revoked, or was already revoked.",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE},
)
async def logout(payload: RefreshRequest, sessions: SessionServiceDep) -> Response:
    """Revokes the session the refresh token belongs to.

    Takes the **refresh token**, not the access token, and that is the
    substantive choice in this endpoint. An access token names a *user*,
    not a session — it has no `sid` claim — so it cannot say which of five
    devices to sign out. The refresh token names exactly one session,
    which is precisely what "sign out of this device" means.

    `204` with no body: there is nothing to say, and a body would invite a
    client to parse it.

    **Idempotent.** Signing out twice succeeds twice. A caller retrying
    after a dropped response must not receive an error for the retry, and
    "the session is already gone" is the outcome it wanted.

    Deliberately does *not* invalidate the access token — nothing can, for
    up to its remaining 15 minutes. That window is the documented cost of
    a stateless token (`JWTSettings`), and closing it needs the `jti`
    denylist recommended for A64-011.6.

    **Also deliberately does not mark the player offline** (A64-013.6).
    Presence is per *player*, not per session, and somebody signing out on
    a laptop may still be signed in on a phone — publishing "offline" here
    would be a falsehood the phone's next refresh silently corrects.
    `POST /auth/logout-all` is the one that means it; short of that, the
    presence window expiring is what marks an absent player absent.
    """
    await sessions.revoke_by_refresh_token(payload.refresh_token, reason=RevocationReason.PLAYER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@auth_router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of every device",
    response_description="Every session for the account was revoked.",
    responses=_UNAUTHORIZED,
)
async def logout_all(
    user: CurrentUser, sessions: SessionServiceDep, presence: PresenceNotificationServiceDep
) -> Response:
    """Revokes every session for the authenticated account.

    Takes the **access token** rather than a refresh token, unlike
    `/logout`, and the asymmetry is deliberate: this operates on the
    account, not on one device, so the credential that names an account is
    the right one. It also means a player whose laptop was stolen can sign
    out everywhere from their phone without holding the laptop's token.

    Revokes the calling session too. "Log out everywhere" that quietly
    excluded the device you asked from is not what anyone means by it —
    the SE-1 exception exists for password changes, where staying signed
    in is the point, and this is not that.

    `204`, and idempotent: revoking nothing succeeds.

    **Marks the player offline** (A64-013.6) — the only endpoint that does,
    because it is the only one that leaves no device able to be present.
    `POST /auth/logout` deliberately does not; see its description.
    """
    revoked = await sessions.revoke_all_sessions(user.id, reason=RevocationReason.PLAYER)

    logger.info(
        "logout_all_completed",
        extra={"user_id": str(user.id), "sessions_revoked": revoked},
    )

    # A64-013.6. **The one place a player is marked offline**, because it is
    # the one place that revokes every session — there is no device left
    # that could be present. `POST /auth/logout` deliberately does not; see
    # its docstring. A64-013.7 makes it the `PresenceOffline` edge too, and
    # emits nothing for a player who was already absent.
    #
    # After the revocation, so a failed sign-out publishes nothing.
    await presence.record_offline(user.id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@auth_router.post(
    "/ws-ticket",
    status_code=status.HTTP_201_CREATED,
    summary="Mint a single-use WebSocket ticket",
    response_description="A ticket valid for seconds, redeemable once.",
    responses={**_UNAUTHORIZED},
)
async def websocket_ticket(
    user: CurrentUser, tickets: WebSocketTicketServiceDep
) -> ApiResponse[WebSocketTicketRead]:
    """Issues the credential `GET /ws` accepts — AD-09.

    An ordinary authenticated route, and that is the design rather than an
    implementation detail: browsers cannot set headers on a WebSocket
    handshake, so the socket's credential has to survive a query string —
    and the only safe thing to put there is a value that is worthless
    seconds later and cannot be replayed.

    **This is not a second authentication mechanism.** The caller has
    already been authenticated by `TokenValidator` in the ordinary way
    (`CurrentUser`); a ticket is downstream of that check rather than
    alongside it, so there remains exactly one thing on this platform that
    decides whether a credential is valid.

    `201`, because a ticket is a resource this call brings into existence
    and each call makes a different one. No `Location` header: the ticket
    is not addressable, which is the point of it.

    Not throttled by its own rule. A client legitimately mints one per
    socket and reconnects on a flaky network; the platform-wide limit is
    what bounds abuse, and a second policy here would be a second place to
    get one number right.
    """
    return build_response(WebSocketTicketRead.of(await tickets.issue(user.id)))


@auth_router.get(
    "/me",
    summary="Read the authenticated account",
    response_description="The caller's own account.",
    responses={**_UNAUTHORIZED, **_NOT_FOUND},
)
async def me(user: CurrentUser, profiles: UserProfileReaderDep) -> ApiResponse[UserRead]:
    """Returns the account the access token speaks for.

    The identity comes from the token's `sub` — no database read is needed
    to know *who* is asking. The read here is for the profile, which the
    token deliberately does not carry: claims are base64 and end up in
    `localStorage` and proxy logs, and a handle copied into a 15-minute
    credential is a copy that can be wrong.

    Includes `email`, because this endpoint is scoped to the caller's own
    account by construction — `sub` cannot name anyone else.

    Returns `404` if the account was deleted while a valid token was still
    in flight. That is a real, if narrow, window: the token stays
    cryptographically valid for its remaining minutes regardless.
    """
    return build_response(await profiles.get_profile(user.id))


@auth_router.post(
    "/email/verify",
    summary="Confirm an email address",
    response_description="The account, now verified.",
    responses={**_UNPROCESSABLE},
)
async def verify_email(
    payload: VerifyEmailRequest,
    verification: EmailVerificationServiceDep,
) -> ApiResponse[UserRead]:
    """Redeems a verification link.

    The `token` is the query parameter from the link in the email. The
    link points at a frontend page; that page posts the value here rather
    than the API reading it from a query string, so the token stays out of
    access logs and browser history on this side.

    **One-time use.** A redeemed token is dead — clicking the same link
    twice returns `422`, not a second success. Every other outstanding
    link for the account dies with it, so a resend chain cannot leave a
    stale link alive behind a used one.

    `422 invalid_verification_token` covers every failure: unknown token,
    already redeemed, expired. They are deliberately indistinguishable —
    the client's action is the same in all three (offer a new link), and
    telling a caller which it was reports on whether a token they hold was
    ever real.

    Not `401`: this endpoint is not about identity and needs no
    authentication. A `401` would send someone who clicked a stale link to
    a sign-in form, which does not fix anything.

    Returns the account with `is_verified` now `true`. Idempotent at the
    account level — an account that was somehow already verified is
    returned unchanged rather than erroring.
    """
    return build_response(await verification.verify_email(payload.token))


@auth_router.post(
    "/email/verify-code",
    summary="Confirm an email address with a six-digit code",
    response_description="The account, now verified.",
    responses={**_UNAUTHORIZED, **_UNPROCESSABLE},
)
async def verify_email_code(
    payload: VerifyCodeRequest,
    user: CurrentUser,
    verification: EmailVerificationServiceDep,
) -> ApiResponse[UserRead]:
    """Redeems the code from the verification email — A64-021.5H §9.

    **Authenticated, and takes no address.** The session says whose
    challenge this is, so a caller cannot verify somebody else's account by
    naming it and cannot discover whether an address has one open. That is
    what makes it safe to distinguish the failures below, where the
    unauthenticated link endpoint deliberately cannot.

    Four outcomes a client acts on differently:

        email_verification_code_invalid            type the current code
        email_verification_code_expired            ask for another
        email_verification_attempts_exceeded       ask for another; the
                                                   challenge is destroyed
        (success)                                  continue

    **Idempotent for an account that is already verified** — §23. A code
    submitted in one tab after another tab or an emailed link succeeded is
    not a mistake, and answering `422` for a state the caller wanted would
    be reporting a race as their error.

    A malformed body is `422` from the schema and **costs no attempt**
    (§10): a client's own bug must not spend one of five guesses.
    """
    return build_response(await verification.verify_code(user.id, payload.code))


@auth_router.post(
    "/email/resend-code",
    # The **IP** rule, not `/email/resend`'s email-scoped one: this request
    # carries no address — the session says whose challenge it is — and an
    # email-scoped rule with no address to key on contributes no subject and
    # therefore limits nothing at all. A guard that reads as protection and
    # counts nothing is worse than none.
    dependencies=[Depends(RESEND_CODE_RATE_LIMIT)],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send another six-digit verification code",
    responses={**_UNAUTHORIZED, **_CONFLICT, **_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def resend_verification_code(
    user: CurrentUser,
    verification: EmailVerificationServiceDep,
) -> ApiResponse[VerificationAccepted]:
    """Issues a fresh code, invalidating the previous one — §11.

    **Authenticated**, unlike `/email/resend`, and the difference is what
    each is for. That one serves somebody who never received a link and may
    never have signed in, so it takes an address and must say nothing. This
    one serves somebody sitting on `/verify-email` inside a session, so
    there is no account to enumerate — and it can therefore tell them the
    two things they need: that they are already verified, or how long until
    another code may be sent.

    `409 email_already_verified` for an account that is done. `409
    email_verification_resend_too_soon` with a `Retry-After` header inside
    the sixty-second cooldown,
    which is measured from a **durable row** rather than from anything in
    process memory — so a reload, a second tab and a second node agree.

    `202 Accepted`: the work is handed to a mail provider and the outcome is
    not known when this returns.
    """
    await verification.resend_code(user.id)
    return build_response(
        VerificationAccepted(detail="A new verification code has been sent to your email address.")
    )


@auth_router.post(
    "/email/resend",
    dependencies=[Depends(RESEND_VERIFICATION_RATE_LIMIT)],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a new verification link",
    response_description="Accepted. The reply is identical whether or not an account exists.",
    responses={**_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def resend_verification(
    payload: ResendVerificationRequest,
    verification: EmailVerificationServiceDep,
) -> ApiResponse[VerificationAccepted]:
    """Sends a fresh verification link, if there is anything to send.

    **The response is identical in every case** — unknown address,
    already-verified account, or a link genuinely sent. That is the
    security property, not an implementation shortcut: this endpoint is
    unauthenticated by necessity (the person who needs it never received
    the first link and may never have signed in), which makes it an
    account-enumeration surface unless it says nothing.

    So it says nothing. No status code, body, or timing distinguishes the
    three outcomes, and the reply is the deliberately vague "if an account
    exists for that address, a verification link has been sent".

    `202 Accepted`, not `200`: the work is handed to a mail provider and
    the outcome is not known when this returns. `202` is the honest code
    for "we have taken this and will act on it".

    Issuing a new link **invalidates the previous one** — database.md §4.5
    keeps at most one live token per account, enforced by a partial unique
    index. A user who clicks an older link after requesting a new one gets
    `422` and should use the newest email.

    A malformed address is `422`, which reveals nothing: an address that
    cannot be valid cannot belong to anyone.
    """
    await verification.resend_verification(payload.email)

    return build_response(
        VerificationAccepted(
            detail=("If an account exists for that address, a verification link has been sent.")
        )
    )


@auth_router.post(
    "/password/forgot",
    dependencies=[Depends(FORGOT_PASSWORD_RATE_LIMIT)],
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Request a password reset link",
    response_description="Accepted. The reply is identical whether or not an account exists.",
    responses={**_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    reset: PasswordResetServiceDep,
) -> Response:
    """Sends a password reset link, if there is anything to send.

    **The response is identical in every case** — unknown address,
    deactivated account, or a link genuinely sent. That is the security
    property, not an implementation shortcut. This endpoint is
    unauthenticated by necessity (the person who needs it cannot sign in),
    which makes it an account-enumeration surface unless it says nothing,
    and the thing it would disclose is more valuable than most: an
    attacker probing here is deciding which addresses are worth a phishing
    campaign that ends in a password.

    So it says nothing. No status code and no body distinguishes the three
    outcomes. `PasswordResetService` is built so this handler *cannot*
    leak — `forgot_password` returns `None` in every case, so there is
    nothing here to branch on.

    `204 No Content` rather than the `202 Accepted` its sibling
    `/auth/email/resend` returns, and the difference is deliberate rather
    than an inconsistency. `202` carries a body explaining that a link
    *may* have been sent, which is useful when the client wants a sentence
    to render. Here there must be nothing to render that a client could
    accidentally make conditional, and `204` guarantees that by having no
    body at all. The screen after this endpoint says "if an account exists
    for that address, we have sent a link" whatever happened — and that
    sentence belongs to the frontend, which does not need the server's
    permission to display it.

    Issuing a new link **invalidates the previous one** — database.md §4.5
    keeps at most one live token per account, enforced by a partial unique
    index. Somebody who asks twice and then clicks the older email gets
    `422` and should use the newest one.

    A malformed address is `422`, which reveals nothing: an address that
    cannot be valid cannot belong to anyone.

    **Not rate limited.** Nothing here stops an attacker from calling it
    ten thousand times, which is both a mail-bombing vector aimed at one
    person's inbox and the enumeration probe the identical response exists
    to frustrate. That is A64-011.8's task and it is the first
    recommendation this one makes.
    """
    await reset.forgot_password(payload.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@auth_router.post(
    "/password/reset",
    dependencies=[Depends(RESET_PASSWORD_RATE_LIMIT)],
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password using a reset link",
    response_description="The password was replaced and every session was revoked.",
    responses={**_UNPROCESSABLE, **_TOO_MANY_REQUESTS},
)
async def reset_password(
    payload: ResetPasswordRequest,
    reset: PasswordResetServiceDep,
) -> Response:
    """Redeems a password reset link and sets a new password.

    The `token` is the query parameter from the link in the email. The link
    points at a frontend page; that page collects the new password and
    posts both here, rather than the API reading the token from a query
    string, so a credential that replaces passwords stays out of access
    logs and browser history on this side.

    **One-time use.** A redeemed token is dead — submitting the same link
    twice returns `422`, not a second success. Every other outstanding
    reset link for the account dies with it.

    **Every session is revoked, including on devices that did nothing
    wrong.** Refresh tokens stop working, and the person must sign in again
    everywhere with the new password. That is the point rather than a side
    effect: the plausible reason somebody is resetting a password is that
    somebody else knows the old one, and a reset that left the attacker's
    session alive would have achieved nothing.

    Access tokens already issued keep working for up to their remaining 15
    minutes — nothing can revoke a stateless JWT, which is the documented
    cost recorded on `JWTSettings` and the reason the window is short.
    Closing it needs the `jti` denylist recommended since A64-011.6.

    `204` and no body. Deliberately no token pair: this endpoint has
    verified control of an *inbox*, not knowledge of a password, and
    handing back a live session would make an email-account compromise
    silently equivalent to a sign-in. The client's next call is
    `POST /auth/login`.

    `422 invalid_reset_token` covers every token failure: unknown, already
    redeemed, expired. They are deliberately indistinguishable — the
    client's action is the same in all three (ask for a new link), and
    telling a caller which it was reports on whether a token they hold was
    ever real.

    `422 weak_password` means the new password failed the policy, and it is
    returned **whether or not the token was any good** — the schema checks
    the password while parsing, before the token is ever looked up. That
    ordering is what stops this endpoint from becoming a free token
    oracle; see `PasswordResetService`.

    Not `401` in any case: this endpoint is not about identity and needs no
    authentication. A `401` would send somebody who clicked a stale link to
    a sign-in form, which is exactly what they cannot do.
    """
    await reset.reset_password(payload.token, payload.plaintext_password())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
