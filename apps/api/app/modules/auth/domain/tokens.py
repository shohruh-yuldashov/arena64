"""What a token *says* — the claim set, independent of how it is encoded.

Framework-free by rule (architecture.md §8: a `domain/` layer imports no
FastAPI, no SQLAlchemy, no clock). Notably it also imports no `jwt`: JWT
is one encoding of these claims, and putting `TokenClaims` here rather
than in `infrastructure/` is what keeps that true. A future task that
signs a WebSocket ticket (AD-09) as an opaque Redis-backed value rather
than a JWT reuses this vocabulary unchanged.

Deliberately a frozen dataclass rather than a Pydantic model, for the
same reason `users.public.credentials.UserCredentials` is: a Pydantic
model is one keystroke from being a FastAPI `response_model`, and a type
whose whole purpose is to describe a credential must not be that. Nothing
here should ever be serialised to a client — the client already has the
token; what it must never receive is the platform's *reading* of one.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

#: Claim names, spelled once. RFC 7519 §4.1 registers all but `type`;
#: `type` is this platform's private claim (§4.3) and is deliberately
#: *not* spelled `typ`, which is a registered **header** parameter meaning
#: "this is a JWT". Reusing that name in the payload is a documented
#: source of confusion between libraries, and it would put a security
#: decision in a field some JWT tooling rewrites on its own.
CLAIM_SUBJECT = "sub"
CLAIM_TOKEN_ID = "jti"
CLAIM_ISSUED_AT = "iat"
CLAIM_EXPIRES_AT = "exp"
CLAIM_ISSUER = "iss"
CLAIM_AUDIENCE = "aud"
CLAIM_TOKEN_TYPE = "type"

#: The claims this platform sets itself. Anything else in a decoded
#: payload is a custom claim and is preserved in `TokenClaims.custom`.
REGISTERED_CLAIMS = frozenset(
    {
        CLAIM_SUBJECT,
        CLAIM_TOKEN_ID,
        CLAIM_ISSUED_AT,
        CLAIM_EXPIRES_AT,
        CLAIM_ISSUER,
        CLAIM_AUDIENCE,
        CLAIM_TOKEN_TYPE,
    }
)


class TokenType(StrEnum):
    """What a token is *for*.

    The reason this claim exists at all: without it, every token signed by
    this platform is interchangeable with every other, and any long-lived
    token this platform ever signs becomes an access token that lasts as
    long. `TokenValidator` requires the type it expects and rejects
    anything else, so a token can only be redeemed at the endpoint it was
    minted for.

    **Only `ACCESS` exists, and after nine slices that is still correct.**
    A64-011.4's refresh tokens turned out not to be JWTs at all — they are
    opaque, stored, and revocable precisely because they are rows
    (database.md §14.3) — so there was never a `TokenType.REFRESH` to add.
    The WebSocket ticket (AD-09) is the one plausible future member, and
    it is named here rather than declared, for the reason `PasswordHasher`
    published `hash` alone in A64-011.1: an unused member on a security
    interface reads as "this is wired up" to whoever adds the next task,
    and a type that nothing issues and nothing rejects is worse than
    absent. It arrives with its issuer.
    """

    ACCESS = "access"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """One decoded, verified claim set.

    Holding this object means the signature, `exp`, `iss`, `aud` and
    `type` have **all** been checked — `TokenProvider.decode` is the only
    thing that constructs one from a token string, and it raises rather
    than returning a partially-verified result. There is deliberately no
    `is_valid` flag: a validity flag on a credential is an invitation to
    forget to read it.
    """

    subject: UUID
    """`sub` — the account this token speaks for.

    A `UUID` here and a string on the wire, because RFC 7519 §4.1.2
    defines `sub` as a StringOrURI. Parsing it back to a UUID at the
    boundary means no caller downstream handles a string that might not be
    an identifier.
    """

    token_id: UUID
    """`jti` — this token's own identity, unique per issuance.

    Read into `auth.public.AuthenticatedUser`, so a route's log line can
    be joined to the `access_token_issued` line that minted the
    credential. Nothing *enforces* on it yet: it is what a denylist would
    key on, and that denylist is still unbuilt as of A64-011.9.

    The claim is nonetheless carried from day one on purpose. A token
    minted without a `jti` could never be revoked individually, and tokens
    already in circulation cannot be given one retroactively — so the
    claim has to exist before there is something to revoke.
    """

    token_type: TokenType
    issued_at: datetime
    expires_at: datetime
    issuer: str
    audience: str

    custom: Mapping[str, Any] = field(default_factory=dict)
    """Claims this platform did not set — the "support future custom
    claims" seam.

    Preserved on decode rather than discarded, so a claim added by a later
    task (a session id for SE-2's revocation list, a device descriptor, a
    locale for a mobile client) survives a round trip through code written
    before it existed.

    Two rules govern what may go in here, and both are security
    properties rather than style:

    **Nothing secret.** A JWT payload is base64, not encryption. Anyone
    holding the token reads every claim, and tokens end up in
    `localStorage`, proxy logs and bug reports.

    **No personal data.** No email, no handle, no display name. The same
    exposure applies, and a token is a place personal data leaks *and*
    goes stale — a handle is mutable (domain-model.md §7.2), so a copy
    inside a fifteen-minute credential is a copy that can be wrong.
    """

    def is_expired_at(self, instant: datetime) -> bool:
        """Whether this claim set has aged out as of `instant`.

        Takes the instant rather than reading the clock — AD-07 forbids
        the domain from doing that, and it is what makes "expires one
        second from now" a test that runs instantly instead of sleeping.

        Note that `TokenProvider.decode` already enforces expiry, so this
        is not the guard; it is here for callers that hold verified claims
        and need to reason about the remaining window (a refresh decision,
        a WebSocket connection deciding whether to re-authenticate
        mid-session).
        """
        return instant >= self.expires_at
