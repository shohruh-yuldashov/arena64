"""`TokenValidator` — turns a bearer string into proven claims, or raises.

The mirror of `AccessTokenService`, and separate from it for the same
reason the two halves of a signature are separate: issuing happens once
per sign-in, validating happens on **every authenticated request on the
platform**, including — once the gateway exists — every WebSocket frame.
They have wildly different call rates, different callers, and only one of
them needs to be reachable from a dependency that runs before any route
code does.

## Why this is not just a call to `TokenProvider.decode`

Today it is nearly that, and the wrapper still earns its place:

- It names the token type. `decode` demands an `expected_type` precisely
  so nobody can forget it; `validate_access_token` is the one place that
  answers "access", so no route, dependency or future gateway handler
  gets to choose.
- It is where the checks that are *not* cryptographic will go. The
  `jti` denylist is the outstanding one, and it is worth being precise
  that it has **not** shipped: A64-011.3 anticipated it for A64-011.4,
  A64-011.4 through .8 did not add it, and SE-1/SE-3 still require that a
  password change and a suspension take effect immediately. Today they do
  not — a revoked session's *access* token keeps verifying until it
  expires, which is the documented cost of a stateless credential
  (`JWTSettings`) and the reason its window is fifteen minutes. When the
  denylist arrives it lands here, behind an interface every caller already
  uses, rather than being retrofitted into every call site of `decode`.

## What it deliberately does not do

No database read, no user load, no permission check.

The first two are the point of a stateless token: an access token is a
*claim about identity that verifies on its own*, and a round trip to
`users` on every request re-adds exactly the coupling and the latency the
token exists to remove. A route that needs the profile asks for it; a
route that needs only "who is this" pays nothing.

Authorization is out of scope for this task and is a different question
in any case — this answers "who are you", never "may you".
"""

import logging

from app.modules.auth.application.ports import TokenProvider
from app.modules.auth.domain.tokens import TokenClaims, TokenType

logger = logging.getLogger(__name__)


class TokenValidator:
    def __init__(self, *, tokens: TokenProvider) -> None:
        self._tokens = tokens

    def validate_access_token(self, token: str) -> TokenClaims:
        """Verifies an access token and returns its claims.

        Raises `ExpiredToken` when it has aged out, `InvalidSignature`
        when no active key signed it, and `InvalidToken` for every other
        failure — malformed, wrong issuer, wrong audience, missing or
        unparsable claims, or a token of some other type.

        The returned claims are fully verified. There is no state in which
        this returns something a caller must check further before
        trusting the subject.
        """
        return self._tokens.decode(token, expected_type=TokenType.ACCESS)
