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

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import ClockDep, SettingsDep
from app.modules.auth.application.services import TokenValidator
from app.modules.auth.domain.exceptions import MissingToken
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


def get_token_validator(settings: SettingsDep, clock: ClockDep) -> TokenValidator:
    """Builds the validator for this request.

    Per-request construction of two stateless objects, both of which cost
    an attribute assignment — measured against the ~15µs of the HMAC they
    exist to perform, a singleton would be optimising nothing. The same
    measurement A64-011.1 made for the Argon2 hasher, with the same
    conclusion; the hasher was later shared for a *correctness* reason (a
    memo that had to be per-process), and nothing here has one.

    The clock is **injected**, closing the TODO this docstring carried
    from A64-011.3 ("`SystemClock` is constructed rather than injected
    because there is no clock dependency in `app.api.deps` yet"). A64-011.9
    added one, and it matters more here than the note implied: this is the
    clock every access-token expiry check on the platform is measured
    against, and it was the one place a test could not move time without
    patching `datetime` — exactly what AD-07 exists to avoid.
    """
    return TokenValidator(tokens=JwtTokenProvider(settings.jwt, clock))


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


# --- removed in A64-011.9: `require_authentication(request)` -----------------
#
# A module-level function that read `request.state.user` and raised
# `AuthenticationRequired` when it was absent. Removed by the audit for two
# reasons, either of which would have been sufficient.
#
# **Nothing ever set `request.state.user`.** No middleware, no dependency,
# no route — the function's own docstring said so ("the setter arrives with
# whatever first needs it"). It could therefore only ever raise. That is
# the speculative generality CLAUDE.md §1 rule 7 forbids, and this module
# already applies that rule twice elsewhere and says why: `TokenType` ships
# `ACCESS` alone, and `PasswordHasher` shipped `hash` alone, both on the
# grounds that "an unused member on a security interface reads as *this is
# wired up* to whoever adds the next task".
#
# **Its name collided with the guard that is real.** `require_authentication`
# (function, dead, always raises) beside `RequireAuthentication` (class,
# live, the router-level guard) is one shift key apart. A reviewer skimming
# `dependencies=[Depends(require_authentication)]` would see a plausible
# guard, and get an endpoint that returns 401 to everyone — or, had a setter
# ever been added carelessly, one that trusted state no verification wrote.
#
# The seam it was reserving is still worth having, and is cheaper to add
# than to keep wrong: whatever first needs an identity outside a route
# signature adds the setter and the reader together, in one commit, where
# the pair can be reviewed.


#: The two aliases routes actually annotate with.
#:
#:     async def me(user: CurrentUser) -> ...:          # 401 without a token
#:     async def feed(user: OptionalCurrentUser) -> ...: # None without one
CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
OptionalCurrentUser = Annotated[AuthenticatedUser | None, Depends(get_current_user_optional)]
