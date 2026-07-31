"""`JwtTokenProvider` — real PyJWT, real HMAC, no mocking.

Deliberately exercises the actual library rather than stubbing it,
because every property worth asserting here is a property of the real
implementation: that a token this platform signs verifies, and — far more
importantly — that eight specific kinds of token it did *not* sign do
not.

Tokens are forged with bare `jwt.encode` rather than by mutating output
from the provider, because that is how an attacker builds one: choose the
claims, choose the algorithm, sign with whatever key you have. A test
that could only produce well-formed tokens would be testing the happy
path twice.

The clock is fixed and injected. That is not merely convenient — verified
during development, PyJWT's own `exp`/`iat` verification consults
`datetime.now()` directly and made an expiry test pass or fail depending
on the real date, which is why the provider took those checks over. See
`JwtTokenProvider._verified_payload`.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from jwt.utils import base64url_encode
from pydantic import SecretStr

from app.config.settings import JWTSettings
from app.modules.auth.domain.exceptions import ExpiredToken, InvalidSignature, InvalidToken
from app.modules.auth.domain.tokens import TokenClaims, TokenType
from app.modules.auth.infrastructure import JwtTokenProvider

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
SUBJECT = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")
CURRENT_KEY = "current-signing-key-comfortably-over-the-minimum"
PREVIOUS_KEY = "previous-signing-key-comfortably-over-the-minimum"
TTL = 900


class MovableClock:
    """A clock a test can wind forward, so "expired" is an assignment
    rather than a `sleep`."""

    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def settings() -> JWTSettings:
    return JWTSettings(secret_key=SecretStr(CURRENT_KEY))


@pytest.fixture
def provider(settings: JWTSettings, clock: MovableClock) -> JwtTokenProvider:
    return JwtTokenProvider(settings, clock)


def forge(
    *,
    key: str = CURRENT_KEY,
    algorithm: str = "HS256",
    **overrides: Any,
) -> str:
    """Builds a token from scratch with any claim replaced or removed.

    Passing `None` for a claim drops it entirely, which is a different
    attack from setting it wrong: a verifier that only compares present
    claims accepts a token with no `aud` at all.
    """
    payload: dict[str, Any] = {
        "sub": str(SUBJECT),
        "jti": str(uuid4()),
        "type": TokenType.ACCESS.value,
        "iat": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + TTL,
        "iss": "arena64",
        "aud": "arena64-api",
    }
    payload.update(overrides)
    payload = {name: value for name, value in payload.items() if value is not None}
    return jwt.encode(payload, key=key, algorithm=algorithm)


class TestIssue:
    def test_produces_a_decodable_token(self, provider: JwtTokenProvider) -> None:
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        assert provider.decode(token, expected_type=TokenType.ACCESS).subject == SUBJECT

    def test_returns_the_claims_it_just_wrote(self, provider: JwtTokenProvider) -> None:
        """So a caller does not have to decode a token to learn what it
        put in one a microsecond ago."""
        token, claims = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        decoded = provider.decode(token, expected_type=TokenType.ACCESS)

        assert decoded == claims

    def test_sets_every_required_claim(self, provider: JwtTokenProvider) -> None:
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        payload = jwt.decode(token, options={"verify_signature": False})

        assert set(payload) == {"sub", "jti", "type", "iat", "exp", "iss", "aud"}

    def test_the_lifetime_is_exactly_what_was_asked_for(self, provider: JwtTokenProvider) -> None:
        _, claims = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        assert claims.expires_at - claims.issued_at == timedelta(seconds=TTL)

    def test_every_token_gets_a_distinct_id(self, provider: JwtTokenProvider) -> None:
        """`jti` is what a revocation denylist keys on (A64-011.4). Two
        tokens sharing one would mean revoking either revokes both."""
        _, first = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        _, second = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        assert first.token_id != second.token_id

    def test_timestamps_are_whole_seconds(self, provider: JwtTokenProvider) -> None:
        """`exp`/`iat` are NumericDate and carry no sub-second precision.
        Truncating on the way out is what makes the returned claims equal
        to the decoded ones rather than microseconds apart."""
        _, claims = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        assert claims.issued_at.microsecond == 0
        assert claims.expires_at.microsecond == 0

    def test_reads_the_injected_clock_not_the_wall_clock(self, provider: JwtTokenProvider) -> None:
        _, claims = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        assert claims.issued_at == NOW


class TestCustomClaims:
    def test_custom_claims_survive_a_round_trip(self, provider: JwtTokenProvider) -> None:
        """The "support future custom claims" seam: a claim added by a
        later task must survive code written before it existed."""
        token, _ = provider.issue(
            subject=str(SUBJECT),
            token_type=TokenType.ACCESS,
            lifetime_seconds=TTL,
            custom={"sid": "session-1", "device": "web"},
        )

        decoded = provider.decode(token, expected_type=TokenType.ACCESS)

        assert decoded.custom == {"sid": "session-1", "device": "web"}

    def test_registered_claims_are_absent_from_custom(self, provider: JwtTokenProvider) -> None:
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        assert provider.decode(token, expected_type=TokenType.ACCESS).custom == {}

    @pytest.mark.parametrize(
        "smuggled",
        [
            pytest.param({"sub": str(uuid4())}, id="sub"),
            pytest.param({"exp": 99999999999}, id="exp"),
            pytest.param({"type": "refresh"}, id="type"),
            pytest.param({"aud": "somewhere-else"}, id="aud"),
        ],
    )
    def test_a_custom_claim_cannot_overwrite_a_registered_one(
        self, provider: JwtTokenProvider, smuggled: dict[str, Any]
    ) -> None:
        """Otherwise `custom` is a hole straight through every guarantee
        the platform's own claims provide — a caller could mint a token
        for another subject, or one that never expires."""
        token, claims = provider.issue(
            subject=str(SUBJECT),
            token_type=TokenType.ACCESS,
            lifetime_seconds=TTL,
            custom=smuggled,
        )
        decoded = provider.decode(token, expected_type=TokenType.ACCESS)

        assert decoded.subject == SUBJECT
        assert decoded.token_type is TokenType.ACCESS
        assert decoded.expires_at == claims.expires_at
        assert decoded.audience == "arena64-api"


class TestExpiry:
    def test_a_token_is_valid_up_to_the_last_second(
        self, provider: JwtTokenProvider, clock: MovableClock
    ) -> None:
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        clock.instant = NOW + timedelta(seconds=TTL - 1)

        assert provider.decode(token, expected_type=TokenType.ACCESS).subject == SUBJECT

    def test_a_token_is_expired_at_exactly_exp(
        self, provider: JwtTokenProvider, clock: MovableClock
    ) -> None:
        """`exp` is the instant it stops being valid, not the last valid
        instant — RFC 7519 §4.1.4: the token must not be accepted "on or
        after" it. An off-by-one here is a token that lives a second past
        its stated life, every time."""
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        clock.instant = NOW + timedelta(seconds=TTL)

        with pytest.raises(ExpiredToken):
            provider.decode(token, expected_type=TokenType.ACCESS)

    def test_an_expired_token_raises_expired_not_invalid(
        self, provider: JwtTokenProvider, clock: MovableClock
    ) -> None:
        """The distinction the client acts on: refresh and retry, rather
        than discard and send the user back to a sign-in form."""
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        clock.instant = NOW + timedelta(days=1)

        with pytest.raises(ExpiredToken) as raised:
            provider.decode(token, expected_type=TokenType.ACCESS)
        assert raised.value.code.value == "expired_token"

    def test_expiry_follows_the_injected_clock(
        self, provider: JwtTokenProvider, clock: MovableClock
    ) -> None:
        """Winding the clock back makes an expired token valid again,
        which proves nothing consults the wall clock behind our back."""
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        clock.instant = NOW + timedelta(days=1)
        with pytest.raises(ExpiredToken):
            provider.decode(token, expected_type=TokenType.ACCESS)

        clock.instant = NOW
        assert provider.decode(token, expected_type=TokenType.ACCESS).subject == SUBJECT

    def test_a_token_dated_in_the_future_is_accepted(self, provider: JwtTokenProvider) -> None:
        """Deliberate. With one signing service, a future `iat` can only
        mean clock skew between our own instances — rejecting it turns an
        NTP wobble into signed-out users. The signature already proves the
        token is ours."""
        token = forge(iat=int((NOW + timedelta(hours=1)).timestamp()))

        assert provider.decode(token, expected_type=TokenType.ACCESS).subject == SUBJECT


class TestSignature:
    def test_a_token_signed_with_another_key_is_rejected(self, provider: JwtTokenProvider) -> None:
        with pytest.raises(InvalidSignature):
            provider.decode(
                forge(key="an-entirely-different-key-of-ample-length"),
                expected_type=TokenType.ACCESS,
            )

    def test_a_tampered_payload_is_rejected(self, provider: JwtTokenProvider) -> None:
        """The attack the signature exists to stop: take a real token,
        swap the subject, keep the signature."""
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        header, _, signature = token.split(".")
        forged_payload = base64url_encode(
            b'{"sub":"019fb9ea-0a0c-7cec-9c5f-402727c31a97","jti":"019fb9ea-0a0c-7cec-9c5f-402727c31a98",'
            b'"type":"access","iat":1,"exp":99999999999,"iss":"arena64","aud":"arena64-api"}'
        ).decode()

        with pytest.raises(InvalidSignature):
            provider.decode(
                f"{header}.{forged_payload}.{signature}", expected_type=TokenType.ACCESS
            )

    def test_an_unsigned_token_is_rejected(self, provider: JwtTokenProvider) -> None:
        """`alg: none` — the canonical JWT break. A verifier that reads the
        algorithm out of the token it is verifying accepts this."""
        with pytest.raises(InvalidToken):
            provider.decode(forge(key="", algorithm="none"), expected_type=TokenType.ACCESS)

    def test_the_signature_alone_is_not_stripped(self, provider: JwtTokenProvider) -> None:
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        header, payload, _ = token.split(".")

        with pytest.raises(InvalidToken):
            provider.decode(f"{header}.{payload}.", expected_type=TokenType.ACCESS)


class TestIssuerAndAudience:
    def test_a_token_from_another_issuer_is_rejected(self, provider: JwtTokenProvider) -> None:
        with pytest.raises(InvalidToken):
            provider.decode(forge(iss="somebody-else"), expected_type=TokenType.ACCESS)

    def test_a_token_for_another_audience_is_rejected(self, provider: JwtTokenProvider) -> None:
        """What stops a token minted for one verifier being replayed at
        another — concrete the moment `auth` also mints WebSocket tickets
        (AD-09) or a mobile audience."""
        with pytest.raises(InvalidToken):
            provider.decode(forge(aud="arena64-gateway"), expected_type=TokenType.ACCESS)

    @pytest.mark.parametrize(
        "missing",
        [
            pytest.param("sub", id="sub"),
            pytest.param("jti", id="jti"),
            pytest.param("type", id="type"),
            pytest.param("iat", id="iat"),
            pytest.param("exp", id="exp"),
            pytest.param("iss", id="iss"),
            pytest.param("aud", id="aud"),
        ],
    )
    def test_a_correctly_signed_token_missing_any_claim_is_rejected(
        self, provider: JwtTokenProvider, missing: str
    ) -> None:
        """Absence must fail, not pass vacuously. A verifier that only
        compares claims it finds accepts a token carrying none of them —
        including one with no `exp`, which never expires."""
        dropped: dict[str, Any] = {missing: None}
        with pytest.raises(InvalidToken):
            provider.decode(forge(**dropped), expected_type=TokenType.ACCESS)


class TestMalformedTokens:
    @pytest.mark.parametrize(
        "token",
        [
            pytest.param("", id="empty"),
            pytest.param("not-a-token", id="no-dots"),
            pytest.param("only.two", id="two-segments"),
            pytest.param("a.b.c.d", id="four-segments"),
            pytest.param("!!!.???.***", id="not-base64"),
            pytest.param("eyJhbGciOiJIUzI1NiJ9.bm90LWpzb24.sig", id="payload-not-json"),
            pytest.param("." * 2, id="empty-segments"),
        ],
    )
    def test_garbage_is_rejected_as_invalid(self, provider: JwtTokenProvider, token: str) -> None:
        with pytest.raises(InvalidToken):
            provider.decode(token, expected_type=TokenType.ACCESS)

    @pytest.mark.parametrize(
        ("claim", "value"),
        [
            pytest.param("sub", "administrator", id="sub-not-a-uuid"),
            pytest.param("sub", {"$ne": None}, id="sub-is-an-object"),
            pytest.param("jti", "not-a-uuid", id="jti-not-a-uuid"),
            pytest.param("type", "refresh", id="type-not-a-member"),
            pytest.param("type", 7, id="type-not-a-string"),
            pytest.param("exp", "tomorrow", id="exp-not-numeric"),
        ],
    )
    def test_a_signed_token_with_an_unparsable_claim_is_rejected(
        self, provider: JwtTokenProvider, claim: str, value: Any
    ) -> None:
        """A valid signature proves the payload was not modified in
        transit — not that this platform wrote it, and not that its claims
        have the types the code expects. Every one of these is a valid JWT
        and none is a usable credential."""
        override: dict[str, Any] = {claim: value}
        with pytest.raises(InvalidToken):
            provider.decode(forge(**override), expected_type=TokenType.ACCESS)


class TestTokenType:
    def test_a_token_of_another_type_is_rejected(self, provider: JwtTokenProvider) -> None:
        """The guard that stops A64-011.4's deliberately long-lived
        refresh token from working as an access token — which is what
        makes its lifetime safe to set."""
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        class _Other(str):
            value = "refresh"

        with pytest.raises(InvalidToken):
            provider.decode(token, expected_type=_Other())  # type: ignore[arg-type]


class TestKeyRotation:
    """dependency-injection.md §2.4: signing keys must be rotatable
    without downtime. Single-key rotation signs every user out at the
    instant of rotation, which makes rotation an incident — and therefore
    something nobody does."""

    def test_a_token_signed_by_the_previous_key_still_verifies(self, clock: MovableClock) -> None:
        before = JwtTokenProvider(JWTSettings(secret_key=SecretStr(PREVIOUS_KEY)), clock)
        token, _ = before.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        during = JwtTokenProvider(
            JWTSettings(
                secret_key=SecretStr(CURRENT_KEY),
                previous_secret_keys=(SecretStr(PREVIOUS_KEY),),
            ),
            clock,
        )

        assert during.decode(token, expected_type=TokenType.ACCESS).subject == SUBJECT

    def test_new_tokens_are_signed_with_the_current_key_only(self, clock: MovableClock) -> None:
        """The old key verifies; it must not sign. Otherwise a rotation
        never actually retires anything."""
        during = JwtTokenProvider(
            JWTSettings(
                secret_key=SecretStr(CURRENT_KEY),
                previous_secret_keys=(SecretStr(PREVIOUS_KEY),),
            ),
            clock,
        )
        token, _ = during.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        # Bare PyJWT, to check the *key* rather than route back through the
        # provider under test. `verify_exp`/`verify_iat` are off for the
        # reason the provider turns them off in production: PyJWT compares
        # them against the wall clock, and this token is dated by the fixed
        # clock above. Left on, this assertion fails with
        # `ImmatureSignatureError` depending on what time of day it runs —
        # observed, not hypothesised.
        assert jwt.decode(
            token,
            CURRENT_KEY,
            algorithms=["HS256"],
            audience="arena64-api",
            issuer="arena64",
            options={"verify_exp": False, "verify_iat": False},
        )
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(
                token,
                PREVIOUS_KEY,
                algorithms=["HS256"],
                audience="arena64-api",
                issuer="arena64",
                options={"verify_exp": False, "verify_iat": False},
            )

    def test_dropping_the_previous_key_completes_the_rotation(self, clock: MovableClock) -> None:
        before = JwtTokenProvider(JWTSettings(secret_key=SecretStr(PREVIOUS_KEY)), clock)
        token, _ = before.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        after = JwtTokenProvider(JWTSettings(secret_key=SecretStr(CURRENT_KEY)), clock)

        with pytest.raises(InvalidSignature):
            after.decode(token, expected_type=TokenType.ACCESS)


class TestClaimsAreOpaqueToTheClient:
    def test_no_personal_data_reaches_the_payload(self, provider: JwtTokenProvider) -> None:
        """A JWT payload is base64, not encryption. Anyone holding the
        token reads every claim, and tokens land in `localStorage`, proxy
        logs and screenshots."""
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )
        payload = jwt.decode(token, options={"verify_signature": False})

        assert "email" not in payload
        assert "username" not in payload
        assert not any("@" in str(value) for value in payload.values())

    def test_the_signing_key_is_not_in_the_token(self, provider: JwtTokenProvider) -> None:
        token, _ = provider.issue(
            subject=str(SUBJECT), token_type=TokenType.ACCESS, lifetime_seconds=TTL
        )

        assert CURRENT_KEY not in token


class TestTokenClaims:
    def test_is_expired_at_takes_the_instant_rather_than_reading_a_clock(self) -> None:
        claims = TokenClaims(
            subject=SUBJECT,
            token_id=uuid4(),
            token_type=TokenType.ACCESS,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=TTL),
            issuer="arena64",
            audience="arena64-api",
        )

        assert claims.is_expired_at(NOW) is False
        assert claims.is_expired_at(NOW + timedelta(seconds=TTL - 1)) is False
        assert claims.is_expired_at(NOW + timedelta(seconds=TTL)) is True
