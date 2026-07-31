"""`AccessTokenService` — mints access tokens. One use case, one type.

Orchestrates; does not compute (services.md §3.2). The signing lives
behind `TokenProvider`, the lifetime in `JWTSettings`. What lives here is
the single decision this class exists to own: *what an access token for a
user says*.

## Why this is a service and not a method on the provider

`TokenProvider.issue` will mint whatever it is asked for. This is the one
place that decides an **access** token lasts
`JWTSettings.access_token_ttl_seconds` and carries `TokenType.ACCESS` —
so when A64-011.4 adds `RefreshTokenService`, its very different lifetime
is a different class rather than a different argument someone can pass by
mistake. A single `create_token(type, lifetime)` would put the two most
consequential security parameters on the platform into the hands of every
caller.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.config.settings import JWTSettings
from app.modules.auth.application.ports import TokenProvider
from app.modules.auth.domain.tokens import TokenType
from app.modules.users.public import UserRead

logger = logging.getLogger(__name__)

#: RFC 6750's scheme name, spelled once. Returned to the client so it
#: knows how to present the token back, and matched by the
#: `WWW-Authenticate` challenge on a 401.
BEARER_SCHEME = "Bearer"


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    """A freshly minted access token and everything a client needs to use
    it.

    A plain frozen dataclass, not a Pydantic model, for the reason
    `TokenClaims` and `UserCredentials` are: a credential-bearing type
    must not be one keystroke from being a FastAPI `response_model`. The
    login response of A64-011.4 will declare its own wire schema and copy
    these fields across deliberately.

    `repr=False` on the token itself is not decoration — a dataclass repr
    lands in tracebacks and in every error reporter that walks frame
    locals, and an access token in a bug report is a working credential
    for however long it has left (services.md §8.5).
    """

    token: str = field(repr=False)
    expires_at: datetime
    expires_in_seconds: int
    """Both the instant and the remaining seconds, because clients need
    different ones: a browser schedules a refresh off the duration, and a
    diagnostic wants the absolute instant. Deriving one from the other on
    the client means trusting the client's clock, which is the one clock
    this platform cannot vouch for."""

    token_type: str = BEARER_SCHEME


class AccessTokenService:
    def __init__(self, *, tokens: TokenProvider, settings: JWTSettings) -> None:
        self._tokens = tokens
        self._settings = settings

    def create_access_token(self, user: UserRead) -> IssuedAccessToken:
        """Issues an access token for `user`.

        Takes the published `UserRead` because that is exactly what
        `AuthenticationService.authenticate` returns, so A64-011.4 wires
        login to this in one line rather than unpacking and re-packing an
        identifier.

        **Only `user.id` is read, and only `user.id` reaches the token.**
        Not the email, not the username, not the display name — even
        though all four are sitting right there on the argument. A JWT
        payload is base64, not encryption: every claim is readable by
        anyone holding the token, and tokens end up in `localStorage`,
        proxy logs and screenshots. A handle is also mutable
        (domain-model.md §7.2), so a copy inside a credential is a copy
        that can be wrong. Whatever needs the user's name should read it
        from `users` with the identifier this token proves.

        Nothing here checks whether the account is active or locked. That
        is `AuthenticationService`'s job and it has already run — this is
        called *after* identity is proven, and duplicating the check would
        put a second, driftable copy of the sign-in rules in the module
        that issues tokens.
        """
        token, claims = self._tokens.issue(
            subject=str(user.id),
            token_type=TokenType.ACCESS,
            lifetime_seconds=self._settings.access_token_ttl_seconds,
        )

        # `jti` and `user_id`, never the token and never any part of it.
        # This line is what makes a stolen-token investigation possible —
        # the `jti` in the log is the same one in the credential — without
        # the log itself becoming a place to harvest credentials.
        logger.info(
            "access_token_issued",
            extra={"user_id": str(user.id), "token_id": str(claims.token_id)},
        )

        return IssuedAccessToken(
            token=token,
            expires_at=claims.expires_at,
            expires_in_seconds=self._settings.access_token_ttl_seconds,
        )
