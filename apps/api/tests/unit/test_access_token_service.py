"""`AccessTokenService` and `TokenValidator` — the two use cases.

Both run against the *real* `JwtTokenProvider` rather than a stub. Unlike
Argon2, HMAC is microseconds, so there is nothing to buy by faking it —
and the properties worth asserting here (that the configured lifetime is
the one applied, that the type check is not skippable) are only real if
the token is real.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.config.settings import JWTSettings
from app.core.enums import Locale
from app.modules.auth.application.services import (
    BEARER_SCHEME,
    AccessTokenService,
    TokenValidator,
)
from app.modules.auth.domain.exceptions import ExpiredToken, InvalidToken
from app.modules.auth.domain.tokens import TokenType
from app.modules.auth.infrastructure import JwtTokenProvider
from app.modules.users.public import AvatarReference, UserRead

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
USER_ID = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")
SIGNING_KEY = "a-signing-key-comfortably-over-the-configured-minimum"
EMAIL = "player.one@example.com"


class MovableClock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock()


@pytest.fixture
def settings() -> JWTSettings:
    return JWTSettings(secret_key=SecretStr(SIGNING_KEY))


@pytest.fixture
def provider(settings: JWTSettings, clock: MovableClock) -> JwtTokenProvider:
    return JwtTokenProvider(settings, clock)


@pytest.fixture
def issuer(provider: JwtTokenProvider, settings: JWTSettings) -> AccessTokenService:
    return AccessTokenService(tokens=provider, settings=settings)


@pytest.fixture
def validator(provider: JwtTokenProvider) -> TokenValidator:
    return TokenValidator(tokens=provider)


def account() -> UserRead:
    """A full profile DTO — deliberately populated, so the tests below can
    assert that none of it reaches the token."""
    return UserRead(
        id=USER_ID,
        username="player_one",
        email=EMAIL,
        display_name="Player One",
        bio=None,
        country=None,
        avatar=AvatarReference(
            object_key="avatars/019fb9ea-0a0c-7cec-9c5f-402727c31a96/abc.webp",
            version=2,
            uploaded_at=NOW,
        ),
        preferred_language=Locale.EN,
        timezone="UTC",
        is_active=True,
        is_verified=True,
        created_at=NOW,
        updated_at=None,
    )


class TestCreateAccessToken:
    def test_returns_a_token_the_validator_accepts(
        self, issuer: AccessTokenService, validator: TokenValidator
    ) -> None:
        issued = issuer.create_access_token(account())

        assert validator.validate_access_token(issued.token).subject == USER_ID

    def test_applies_the_configured_lifetime(self, issuer: AccessTokenService) -> None:
        """Not a lifetime the caller passed. The class exists so that the
        access token's window is a configuration decision rather than an
        argument any call site can vary."""
        issued = issuer.create_access_token(account())

        assert issued.expires_in_seconds == JWTSettings().access_token_ttl_seconds
        assert issued.expires_at == NOW + timedelta(seconds=issued.expires_in_seconds)

    def test_a_shorter_configured_lifetime_is_honoured(self, clock: MovableClock) -> None:
        settings = JWTSettings(secret_key=SecretStr(SIGNING_KEY), access_token_ttl_seconds=60)
        issued = AccessTokenService(
            tokens=JwtTokenProvider(settings, clock), settings=settings
        ).create_access_token(account())

        assert issued.expires_at == NOW + timedelta(seconds=60)

    def test_announces_the_bearer_scheme(self, issuer: AccessTokenService) -> None:
        """So a client knows how to present it back, and so the value
        matches the `WWW-Authenticate` challenge a 401 carries."""
        assert issuer.create_access_token(account()).token_type == BEARER_SCHEME

    def test_issues_an_access_token_not_some_other_type(
        self, issuer: AccessTokenService, provider: JwtTokenProvider
    ) -> None:
        issued = issuer.create_access_token(account())

        assert (
            provider.decode(issued.token, expected_type=TokenType.ACCESS).token_type
            is TokenType.ACCESS
        )

    def test_no_profile_data_reaches_the_token(self, issuer: AccessTokenService) -> None:
        """The service is handed a whole `UserRead` and must copy exactly
        one field out of it. Everything else is personal data in a
        base64-readable credential that lands in `localStorage` and proxy
        logs — and a handle is mutable, so a copy inside a token is a copy
        that can be wrong."""
        issued = issuer.create_access_token(account())

        assert EMAIL not in issued.token
        assert "player_one" not in issued.token
        assert "Player One" not in issued.token
        assert "cdn.example.com" not in issued.token

    def test_the_token_is_absent_from_the_repr(self, issuer: AccessTokenService) -> None:
        """A dataclass repr lands in tracebacks and in every error
        reporter that walks frame locals; an access token in a bug report
        is a working credential (services.md §8.5)."""
        issued = issuer.create_access_token(account())

        assert issued.token not in repr(issued)

    def test_two_issuances_produce_distinct_tokens(self, issuer: AccessTokenService) -> None:
        first = issuer.create_access_token(account())
        second = issuer.create_access_token(account())

        assert first.token != second.token


class TestIssuanceLogging:
    def test_logs_the_user_and_token_id(
        self,
        issuer: AccessTokenService,
        validator: TokenValidator,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """What makes a stolen-token investigation possible: the `jti` in
        the log is the same one inside the credential."""
        with caplog.at_level(logging.INFO):
            issued = issuer.create_access_token(account())

        claims = validator.validate_access_token(issued.token)
        record = next(r for r in caplog.records if r.message == "access_token_issued")

        assert record.user_id == str(USER_ID)  # type: ignore[attr-defined]
        assert record.token_id == str(claims.token_id)  # type: ignore[attr-defined]

    def test_never_logs_the_token_itself(
        self, issuer: AccessTokenService, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Otherwise the log becomes a place to harvest live credentials."""
        with caplog.at_level(logging.DEBUG):
            issued = issuer.create_access_token(account())

        assert issued.token not in caplog.text

    def test_never_logs_the_signing_key(
        self, issuer: AccessTokenService, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            issuer.create_access_token(account())

        assert SIGNING_KEY not in caplog.text


class TestValidateAccessToken:
    def test_returns_fully_verified_claims(
        self, issuer: AccessTokenService, validator: TokenValidator
    ) -> None:
        issued = issuer.create_access_token(account())
        claims = validator.validate_access_token(issued.token)

        assert claims.subject == USER_ID
        assert claims.token_type is TokenType.ACCESS
        assert claims.issuer == "arena64"
        assert claims.audience == "arena64-api"

    def test_rejects_an_expired_token(
        self, issuer: AccessTokenService, validator: TokenValidator, clock: MovableClock
    ) -> None:
        issued = issuer.create_access_token(account())
        clock.instant = issued.expires_at

        with pytest.raises(ExpiredToken):
            validator.validate_access_token(issued.token)

    def test_rejects_a_token_this_platform_did_not_sign(
        self, validator: TokenValidator, clock: MovableClock
    ) -> None:
        foreign = JwtTokenProvider(
            JWTSettings(secret_key=SecretStr("a-different-key-of-entirely-sufficient-length")),
            clock,
        )
        token, _ = foreign.issue(
            subject=str(uuid4()), token_type=TokenType.ACCESS, lifetime_seconds=900
        )

        with pytest.raises(InvalidToken):
            validator.validate_access_token(token)

    def test_rejects_garbage(self, validator: TokenValidator) -> None:
        with pytest.raises(InvalidToken):
            validator.validate_access_token("Bearer-looking-but-not-a-token")

    def test_the_expected_type_is_not_a_caller_choice(self, validator: TokenValidator) -> None:
        """`validate_access_token` takes only the token. Nothing about its
        signature lets a route, a dependency or a future gateway handler
        accept a token of a different type — which is what will make
        A64-011.4's long-lived refresh token safe to issue."""
        import inspect

        parameters = set(inspect.signature(validator.validate_access_token).parameters)

        assert parameters == {"token"}
