"""The `TokenProvider` adapter — PyJWT, HMAC, one algorithm.

## The three ways JWT verification is usually got wrong

**Letting the token choose its own algorithm.** A JWT's header names the
algorithm, and a library that trusts it will happily verify a token whose
header says `{"alg":"none"}` — against nothing. The fix is not to check
the header; it is never to read it. `jwt.decode` is called with an
explicit `algorithms=` allowlist built from configuration
(`SUPPORTED_JWT_ALGORITHMS`), so a token asserting `none`, or asserting
`RS256` in the hope that the HMAC secret is treated as a public key, is
rejected before any signature computation happens.

**Verifying the signature and stopping there.** A correctly-signed token
is proof of origin, not of applicability. Without `iss`/`aud` checks, a
token this platform signs for one purpose is redeemable at every other —
which becomes concrete the moment `auth` also mints WebSocket tickets
(AD-09) or a mobile audience. Both are verified here, always, with no
argument to turn them off.

**Trusting claim *types*.** `sub` and `jti` arrive as whatever JSON the
payload contained. A payload with `"sub": {"$ne": null}` or `"exp":
"tomorrow"` is well-formed JSON and a valid JWT; it is the parsing back
into `UUID` and `datetime` below that rejects it, and every one of those
parses is inside the try/except that produces `InvalidToken`.

## What this deliberately does not do

No `verify=False` path, no `options={"verify_signature": False}`, no
"decode without checking for logging". Those exist in most JWT wrappers
and each one is a loaded gun pointed at the next person who needs to read
a claim in a hurry. Anything that needs the subject of an unverified
token does not need the subject of an unverified token.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import jwt

from app.config.settings import SUPPORTED_JWT_ALGORITHMS, JWTSettings
from app.core.clock import Clock
from app.core.identifiers import generate_uuid7
from app.modules.auth.domain.exceptions import ExpiredToken, InvalidSignature, InvalidToken
from app.modules.auth.domain.tokens import (
    CLAIM_AUDIENCE,
    CLAIM_EXPIRES_AT,
    CLAIM_ISSUED_AT,
    CLAIM_ISSUER,
    CLAIM_SUBJECT,
    CLAIM_TOKEN_ID,
    CLAIM_TOKEN_TYPE,
    REGISTERED_CLAIMS,
    TokenClaims,
    TokenType,
)

logger = logging.getLogger(__name__)

#: The message every rejection carries, whatever actually failed. One
#: string, so the response cannot be used to walk a forgery towards a
#: valid shape — see `InvalidToken`'s docstring. The detail goes to the
#: log instead.
_GENERIC_REJECTION = "The access token is not valid."

_REQUIRED_CLAIMS: Final = sorted(REGISTERED_CLAIMS)


class JwtTokenProvider:
    """The production `TokenProvider`.

    Holds the settings and a `Clock`. The clock is injected rather than
    read (AD-07) and it is what makes "this token expired one second ago"
    a test that runs instantly — the alternative, issuing a token with a
    one-second lifetime and sleeping, is the kind of test that gets
    deleted the first time CI is slow.

    Cheap to construct and stateless, so it can be built per request; it
    is nonetheless a process singleton at the composition root, because
    there is nothing to gain from rebuilding it and `PasswordHasher`
    already established that shape.
    """

    def __init__(self, settings: JWTSettings, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock

    # --- issuing ------------------------------------------------------------

    def issue(
        self,
        *,
        subject: str,
        token_type: TokenType,
        lifetime_seconds: int,
        custom: Mapping[str, Any] | None = None,
    ) -> tuple[str, TokenClaims]:
        issued_at = self._clock.now()
        # Whole seconds. `exp` and `iat` are NumericDate (RFC 7519 §2) and
        # carry no sub-second precision, so truncating here means the
        # `TokenClaims` returned to the caller says exactly what a later
        # decode will say — rather than differing by microseconds and
        # making an equality assertion in a test mysteriously fail.
        issued_at = issued_at.replace(microsecond=0)
        expires_at = datetime.fromtimestamp(int(issued_at.timestamp()) + lifetime_seconds, tz=UTC)

        token_id = generate_uuid7()
        claims = TokenClaims(
            subject=UUID(subject),
            token_id=token_id,
            token_type=token_type,
            issued_at=issued_at,
            expires_at=expires_at,
            issuer=self._settings.issuer,
            audience=self._settings.audience,
            custom=dict(custom or {}),
        )

        payload: dict[str, Any] = {
            # Custom claims first, so a caller cannot smuggle a `sub` or an
            # `exp` past the platform's own by passing it in `custom` — the
            # registered claims below overwrite anything of the same name.
            **claims.custom,
            CLAIM_SUBJECT: subject,
            CLAIM_TOKEN_ID: str(token_id),
            CLAIM_TOKEN_TYPE: token_type.value,
            CLAIM_ISSUED_AT: int(issued_at.timestamp()),
            CLAIM_EXPIRES_AT: int(expires_at.timestamp()),
            CLAIM_ISSUER: self._settings.issuer,
            CLAIM_AUDIENCE: self._settings.audience,
        }

        token = jwt.encode(
            payload,
            self._settings.secret_key.get_secret_value(),
            algorithm=self._settings.algorithm,
        )
        return token, claims

    # --- verifying ----------------------------------------------------------

    def decode(self, token: str, *, expected_type: TokenType) -> TokenClaims:
        payload = self._verified_payload(token)
        claims = self._to_claims(payload)

        # Expiry is checked *here*, against the injected clock — not by
        # PyJWT. See `_verified_payload` on why that is not a preference.
        if claims.is_expired_at(self._clock.now()):
            logger.debug("token_rejected", extra={"reason": "expired"})
            raise ExpiredToken("The access token has expired.")

        if claims.token_type is not expected_type:
            # A token redeemed at the wrong door. Worth a distinct log
            # line — in steady state this is either a client bug or
            # somebody trying a refresh token where an access token goes.
            logger.debug(
                "token_rejected",
                extra={"reason": "wrong_type", "expected": expected_type.value},
            )
            raise InvalidToken(_GENERIC_REJECTION)

        return claims

    def _verified_payload(self, token: str) -> dict[str, Any]:
        """Runs PyJWT against each active key until one verifies.

        **`verify_exp` and `verify_iat` are off, and expiry is enforced by
        `decode` instead.** Not a weakening — a relocation, and a
        necessary one. PyJWT compares those claims against
        `datetime.now(timezone.utc)` directly, with no way to supply a
        clock. Left on, two things follow that this platform has already
        ruled out:

        - AD-07 becomes a fiction. The `Clock` port is injected, the
          service reads it for `iat`, and then the single most important
          time comparison on the credential path silently consults the
          system clock anyway.
        - Expiry tests stop being deterministic. Verified directly: a
          fixed-clock test issuing a token dated a few hours from the real
          wall clock is rejected with `ImmatureSignatureError` — a test
          that passes or fails depending on what time of day CI runs.

        `iat` is deliberately *not* re-checked against the clock in either
        direction. PyJWT's version rejects a token whose `iat` is in the
        future, which with one signing service can only mean skew between
        our own instances — turning a routine NTP wobble into signed-out
        users. `iat` is retained because it is audit data and because
        A64-011.4's refresh rotation will want it; it is not a guard.

        The signature, `iss`, `aud` and the presence of every required
        claim remain PyJWT's, because none of them involve time.
        """
        last_signature_error: jwt.InvalidSignatureError | None = None

        for key in self._settings.verification_keys:
            try:
                return dict(
                    jwt.decode(
                        token,
                        key.get_secret_value(),
                        # The allowlist, not the token's header. This one
                        # argument is what makes `alg: none` and
                        # algorithm-confusion unreachable.
                        algorithms=sorted(SUPPORTED_JWT_ALGORITHMS),
                        issuer=self._settings.issuer,
                        audience=self._settings.audience,
                        options={
                            # PyJWT verifies `aud`/`iss` only when told to,
                            # and treats a claim that is absent entirely as
                            # nothing to check unless it is required. Both
                            # halves are needed: a token with no `aud` must
                            # fail, not pass vacuously.
                            "require": _REQUIRED_CLAIMS,
                            "verify_signature": True,
                            "verify_aud": True,
                            "verify_iss": True,
                            # Enforced in `decode` against the injected
                            # clock — see this method's docstring.
                            "verify_exp": False,
                            "verify_iat": False,
                        },
                    )
                )
            except jwt.InvalidSignatureError as error:
                # The only error worth trying the next key for — this is
                # exactly the rotation case.
                last_signature_error = error
                continue
            except jwt.InvalidTokenError as error:
                # Everything else PyJWT raises: malformed, wrong `iss`,
                # wrong `aud`, missing a required claim, bad `iat`.
                logger.debug("token_rejected", extra={"reason": type(error).__name__})
                raise InvalidToken(_GENERIC_REJECTION) from error

        # Every active key was tried and none verified.
        logger.info(
            "token_signature_rejected",
            extra={"keys_tried": len(self._settings.verification_keys)},
        )
        raise InvalidSignature(_GENERIC_REJECTION) from last_signature_error

    def _to_claims(self, payload: dict[str, Any]) -> TokenClaims:
        """Parses a *verified* payload into the domain's claim set.

        Everything below can still fail, and the failures are not
        theoretical: the signature proves the payload was not modified in
        transit, not that this platform wrote it. A token signed with a
        leaked key, or one issued by an older version of this code, can
        carry a `sub` that is not a UUID or a `type` this build has never
        heard of. Each of those is an `InvalidToken`, not a `500`.
        """
        try:
            token_type = TokenType(payload[CLAIM_TOKEN_TYPE])
            return TokenClaims(
                subject=UUID(str(payload[CLAIM_SUBJECT])),
                token_id=UUID(str(payload[CLAIM_TOKEN_ID])),
                token_type=token_type,
                issued_at=datetime.fromtimestamp(int(payload[CLAIM_ISSUED_AT]), tz=UTC),
                expires_at=datetime.fromtimestamp(int(payload[CLAIM_EXPIRES_AT]), tz=UTC),
                issuer=str(payload[CLAIM_ISSUER]),
                audience=str(payload[CLAIM_AUDIENCE]),
                custom={
                    name: value for name, value in payload.items() if name not in REGISTERED_CLAIMS
                },
            )
        except (KeyError, ValueError, TypeError) as error:
            logger.debug("token_rejected", extra={"reason": "unparsable_claims"})
            raise InvalidToken(_GENERIC_REJECTION) from error
