"""The authentication dependencies every protected route will use.

dependency-injection.md DI-01: `Depends` appears only at the routing
layer, to hand a route an already-resolved value. That rule is why the
extraction and verification below live in `presentation/` while the
verification *logic* lives in `application/` — a service that knew what
an `Authorization` header was would be a service that could only be
called over HTTP, and AD-09's gateway and a future mobile client are both
callers that are not.

## The three shapes, and when each is right

    get_current_user            401 unless a valid token is present
    get_current_user_optional   `None` instead of 401 — for endpoints
                                that render differently when signed in
    RequireAuthentication()     a route/router-level guard that yields
                                nothing, for when the handler does not
                                need the identity, only the gate

The third is not redundant with the first. A router that protects twenty
endpoints wants `dependencies=[Depends(RequireAuthentication())]` once at
the `APIRouter`, not an unused `CurrentUser` parameter threaded through
twenty signatures — and an unused parameter is one a later edit deletes,
silently unprotecting the route.

## Why `auto_error=False`

FastAPI's `HTTPBearer` raises its own `HTTPException` when the header is
missing or malformed, which would bypass `app/api/exception_handlers.py`
entirely and return FastAPI's native `{"detail": ...}` shape — the exact
envelope break A64-010 had to fix for request validation. With
`auto_error=False` the scheme becomes what it should be: a declaration
for OpenAPI (it is what puts the padlock in the docs and makes "Authorize"
work) plus a header parser, with every rejection routed through the
platform's own taxonomy.
"""

import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import SettingsDep
from app.core.clock import SystemClock
from app.modules.auth.application.services import TokenValidator
from app.modules.auth.domain.exceptions import AuthenticationRequired, MissingToken
from app.modules.auth.infrastructure import JwtTokenProvider
from app.modules.auth.public import AuthenticatedUser

logger = logging.getLogger(__name__)

#: `auto_error=False` — see this module's docstring. `HTTPBearer` also
#: rejects a header whose scheme is not `Bearer`, so `Authorization: Basic
#: ...` arrives here as `None` rather than as a token that then fails to
#: parse.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="Arena64 access token",
    description="A JWT access token from `POST /auth/login`, as `Bearer <token>`.",
)

BearerCredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def get_token_validator(settings: SettingsDep) -> TokenValidator:
    """Builds the validator for this request.

    Per-request construction of two stateless objects, both of which cost
    an attribute assignment — measured against the ~15µs of the HMAC they
    exist to perform, a singleton would be optimising nothing. The same
    measurement A64-011.1 made for the Argon2 hasher, with the same
    conclusion; the hasher was later shared for a *correctness* reason (a
    memo that had to be per-process), and nothing here has one.

    `SystemClock` is constructed rather than injected because there is no
    clock dependency in `app.api.deps` yet — `users` builds its own the
    same way. When one is added, this becomes a parameter.
    """
    return TokenValidator(tokens=JwtTokenProvider(settings.jwt, SystemClock()))


TokenValidatorDep = Annotated[TokenValidator, Depends(get_token_validator)]


def get_current_user_optional(
    credentials: BearerCredentialsDep,
    validator: TokenValidatorDep,
) -> AuthenticatedUser | None:
    """The identity behind this request, or `None` if there is not one.

    `None` **only** when no bearer credential was presented at all. A
    token that was presented and is invalid still raises — quietly
    downgrading a forged or expired token to "anonymous" is how an
    endpoint ends up serving the signed-out view to someone whose session
    just expired, with no error anywhere to explain it, and how a
    tampered token becomes indistinguishable from no token in the logs.
    """
    if credentials is None:
        return None

    claims = validator.validate_access_token(credentials.credentials)
    return AuthenticatedUser(
        id=claims.subject,
        token_id=claims.token_id,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )


def get_current_user(
    user: Annotated[AuthenticatedUser | None, Depends(get_current_user_optional)],
) -> AuthenticatedUser:
    """The identity behind this request. Raises `MissingToken` if absent.

    Built on the optional form rather than duplicating it, so there is
    exactly one place that knows how a token is extracted and verified.
    Two copies of that is how one of them ends up without the type check.
    """
    if user is None:
        raise MissingToken("This endpoint requires authentication.")
    return user


class RequireAuthentication:
    """A router- or route-level guard: authenticated, or 401.

    A callable class rather than a bare function because FastAPI's
    `dependencies=[...]` takes dependables, and a class gives the guard a
    name that reads as intent at the call site::

        router = APIRouter(dependencies=[Depends(RequireAuthentication())])

    Yields nothing. A route that also wants the identity asks for
    `CurrentUser` in its signature; FastAPI resolves the shared
    sub-dependency once per request, so the token is verified once even
    when both are present.

    Instantiated rather than used bare (`RequireAuthentication()`, not
    `RequireAuthentication`) so that a future parameter — a required scope
    or audience once authorization exists — is an argument rather than a
    breaking change at every call site.
    """

    def __call__(self, user: Annotated[AuthenticatedUser, Depends(get_current_user)]) -> None:
        return None


def require_authentication(request: Request) -> AuthenticatedUser:
    """Reads the identity a guard already established, from anywhere.

    For code that runs outside a route signature — an exception handler, a
    middleware, a future WebSocket frame dispatcher — and must not
    re-verify. Raises `AuthenticationRequired` if nothing has been
    established, rather than silently returning `None` and letting the
    caller treat an unauthenticated request as authenticated.

    Nothing populates `request.state.user` yet; the setter arrives with
    whatever first needs it. It is defined now because the alternative —
    each such caller reaching for the `Authorization` header itself — is
    how a second, subtly different verification path gets written.
    """
    user = getattr(request.state, "user", None)
    if not isinstance(user, AuthenticatedUser):
        raise AuthenticationRequired("This endpoint requires authentication.")
    return user


#: The two aliases routes actually annotate with.
#:
#:     async def me(user: CurrentUser) -> ...:          # 401 without a token
#:     async def feed(user: OptionalCurrentUser) -> ...: # None without one
CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
OptionalCurrentUser = Annotated[AuthenticatedUser | None, Depends(get_current_user_optional)]
