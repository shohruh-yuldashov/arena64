"""`RefreshTokenService` and the `UserSession` entity.

The service is three functions, and every one of them is a place this
goes quietly wrong: a token from the wrong random source looks identical
to a good one, a hash compared with `==` behaves identically to one
compared properly, and a digest stored as hex instead of bytes matches
nothing while raising no error. None of those fail visibly, so they are
asserted here.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.config.settings import REFRESH_TOKEN_MIN_ENTROPY_BYTES, SessionSettings
from app.modules.auth.application.services import RefreshTokenService
from app.modules.auth.domain.sessions import (
    RevocationReason,
    SessionDevice,
    UserSession,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
USER_ID = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")
THIRTY_DAYS = timedelta(days=30)


@pytest.fixture
def settings() -> SessionSettings:
    return SessionSettings()


@pytest.fixture
def tokens(settings: SessionSettings) -> RefreshTokenService:
    return RefreshTokenService(settings)


class TestGenerateRefreshToken:
    def test_carries_at_least_256_bits(self, tokens: RefreshTokenService) -> None:
        """DB-24's whole argument for hashing these with SHA-256 rather
        than Argon2id rests on there being no guessable space. Below 256
        bits that argument weakens, which is why the setting has a floor
        and why this asserts the floor is actually reached."""
        token = tokens.generate_refresh_token()

        # `token_urlsafe(n)` yields ~4n/3 base64 characters carrying 8n
        # bits. Asserting on the character count is asserting on the
        # entropy, without re-deriving base64 in the test.
        assert len(token) >= REFRESH_TOKEN_MIN_ENTROPY_BYTES * 4 // 3

    def test_every_token_is_different(self, tokens: RefreshTokenService) -> None:
        """A thousand draws with no repeat. This would not catch a subtly
        biased generator, but it catches the failure that actually
        happens: a seeded or counter-based source."""
        generated = {tokens.generate_refresh_token() for _ in range(1000)}

        assert len(generated) == 1000

    def test_is_url_safe(self, tokens: RefreshTokenService) -> None:
        """The token travels in a `Set-Cookie` header and a JSON body. A
        token needing encoding somewhere is a token that eventually gets
        compared in two different encodings."""
        token = tokens.generate_refresh_token()

        assert token.isascii()
        assert not set(token) & set("+/=\"'; ,\\")

    def test_a_higher_configured_entropy_is_honoured(self) -> None:
        longer = RefreshTokenService(SessionSettings(token_entropy_bytes=64))

        assert len(longer.generate_refresh_token()) > len(
            RefreshTokenService(SessionSettings()).generate_refresh_token()
        )


class TestHashRefreshToken:
    def test_returns_a_32_byte_digest(self, tokens: RefreshTokenService) -> None:
        """`bytes`, not hex. The column is `bytea`, and a hex rendering is
        a second representation of the same value that something will
        eventually compare against the first — the bug that costs a day is
        `"a1b2..." != b"\\xa1\\xb2..."` silently never matching."""
        digest = tokens.hash_refresh_token("a-token")

        assert isinstance(digest, bytes)
        assert len(digest) == 32

    def test_matches_the_reference_implementation(self, tokens: RefreshTokenService) -> None:
        """Proves it is genuinely SHA-256 of the UTF-8 bytes, rather than
        merely a plausible 32-byte value — and pins the encoding, so a
        token containing a non-ASCII character cannot hash differently on
        another platform."""
        token = "a-token-with-ünïcödé"

        assert tokens.hash_refresh_token(token) == hashlib.sha256(token.encode("utf-8")).digest()

    def test_is_deterministic(self, tokens: RefreshTokenService) -> None:
        """Unsalted on purpose: the digest is the lookup key, so the
        refresh path is one indexed query rather than a scan comparing
        candidates."""
        token = "a-token"

        assert tokens.hash_refresh_token(token) == tokens.hash_refresh_token(token)

    def test_different_tokens_hash_differently(self, tokens: RefreshTokenService) -> None:
        assert tokens.hash_refresh_token("a") != tokens.hash_refresh_token("b")

    def test_the_digest_does_not_contain_the_token(self, tokens: RefreshTokenService) -> None:
        """§14.3: "the token itself exists only in transit and in the
        client". A database read must not yield a working credential."""
        token = tokens.generate_refresh_token()

        assert token.encode() not in tokens.hash_refresh_token(token)


class TestVerifyRefreshToken:
    def test_accepts_the_token_the_hash_was_made_from(self, tokens: RefreshTokenService) -> None:
        token = tokens.generate_refresh_token()

        assert tokens.verify_refresh_token(token, tokens.hash_refresh_token(token)) is True

    def test_rejects_a_different_token(self, tokens: RefreshTokenService) -> None:
        stored = tokens.hash_refresh_token(tokens.generate_refresh_token())

        assert tokens.verify_refresh_token(tokens.generate_refresh_token(), stored) is False

    def test_rejects_a_near_miss(self, tokens: RefreshTokenService) -> None:
        """One character different. SHA-256's avalanche means the digests
        share nothing, so this is really asserting that no prefix
        comparison crept in."""
        token = tokens.generate_refresh_token()
        stored = tokens.hash_refresh_token(token)

        assert tokens.verify_refresh_token(token[:-1] + "x", stored) is False

    def test_rejects_an_empty_token(self, tokens: RefreshTokenService) -> None:
        stored = tokens.hash_refresh_token(tokens.generate_refresh_token())

        assert tokens.verify_refresh_token("", stored) is False

    def test_rejects_a_wrong_length_hash_without_raising(self, tokens: RefreshTokenService) -> None:
        """`hmac.compare_digest` raises on mismatched types but not on
        mismatched lengths. A truncated value from a corrupted row must
        come back as "does not match", not as a 500."""
        token = tokens.generate_refresh_token()

        assert tokens.verify_refresh_token(token, b"\x00" * 16) is False


class TestSessionStart:
    def test_starts_its_own_family(self, tokens: RefreshTokenService) -> None:
        """A fresh sign-in is the root of a new rotation chain, so
        `token_family == id`. This is what makes multiple devices
        independent: reuse detection on one cannot reach the other."""
        session = UserSession.start(
            user_id=USER_ID,
            refresh_token_hash=tokens.hash_refresh_token("t"),
            issued_at=NOW,
            lifetime=THIRTY_DAYS,
        )

        assert session.token_family == session.id

    def test_joins_an_existing_family_when_given_one(self, tokens: RefreshTokenService) -> None:
        """The rotation path — the only way a session joins a chain."""
        family = uuid4()
        session = UserSession.start(
            user_id=USER_ID,
            refresh_token_hash=tokens.hash_refresh_token("t"),
            issued_at=NOW,
            lifetime=THIRTY_DAYS,
            token_family=family,
        )

        assert session.token_family == family
        assert session.id != family

    def test_expiry_is_issued_at_plus_lifetime(self, tokens: RefreshTokenService) -> None:
        session = UserSession.start(
            user_id=USER_ID,
            refresh_token_hash=tokens.hash_refresh_token("t"),
            issued_at=NOW,
            lifetime=THIRTY_DAYS,
        )

        assert session.expires_at == NOW + THIRTY_DAYS

    def test_last_used_starts_at_creation(self, tokens: RefreshTokenService) -> None:
        """Not null. A session created by a sign-in has been used; a
        nullable column here would put a `None` check in front of every
        idle comparison for no information gained."""
        session = UserSession.start(
            user_id=USER_ID,
            refresh_token_hash=tokens.hash_refresh_token("t"),
            issued_at=NOW,
            lifetime=THIRTY_DAYS,
        )

        assert session.last_used_at == NOW

    def test_starts_unrevoked(self, tokens: RefreshTokenService) -> None:
        session = UserSession.start(
            user_id=USER_ID,
            refresh_token_hash=tokens.hash_refresh_token("t"),
            issued_at=NOW,
            lifetime=THIRTY_DAYS,
        )

        assert session.is_revoked is False
        assert session.revoked_reason is None

    def test_reads_the_passed_instant_not_the_clock(self, tokens: RefreshTokenService) -> None:
        """AD-07. A domain object that read the clock could not be tested
        at a fixed instant at all."""
        past = datetime(2020, 1, 1, tzinfo=UTC)
        session = UserSession.start(
            user_id=USER_ID,
            refresh_token_hash=tokens.hash_refresh_token("t"),
            issued_at=past,
            lifetime=THIRTY_DAYS,
        )

        assert session.created_at == past

    def test_the_hash_is_absent_from_the_repr(self, tokens: RefreshTokenService) -> None:
        digest = tokens.hash_refresh_token("t")
        session = UserSession.start(
            user_id=USER_ID,
            refresh_token_hash=digest,
            issued_at=NOW,
            lifetime=THIRTY_DAYS,
        )

        assert str(digest) not in repr(session)


def session_at(
    *,
    issued_at: datetime = NOW,
    lifetime: timedelta = THIRTY_DAYS,
    device: SessionDevice | None = None,
) -> UserSession:
    return UserSession.start(
        user_id=USER_ID,
        refresh_token_hash=hashlib.sha256(b"token").digest(),
        issued_at=issued_at,
        lifetime=lifetime,
        device=device,
    )


class TestSessionExpiry:
    def test_is_usable_up_to_the_last_second(self) -> None:
        session = session_at()

        assert session.is_expired_at(NOW + THIRTY_DAYS - timedelta(seconds=1)) is False

    def test_is_expired_at_exactly_expires_at(self) -> None:
        """`expires_at` is when it stops being valid, not the last instant
        it is. An off-by-one here is a credential living a second past its
        stated life, every time."""
        session = session_at()

        assert session.is_expired_at(NOW + THIRTY_DAYS) is True

    def test_idle_expiry_is_measured_from_last_use(self) -> None:
        idle = timedelta(days=14)
        session = session_at()

        assert session.is_idle_at(NOW + timedelta(days=13), idle) is False
        assert session.is_idle_at(NOW + timedelta(days=14), idle) is True

    def test_touching_slides_the_idle_window(self) -> None:
        idle = timedelta(days=14)
        session = session_at()
        assert session.is_idle_at(NOW + timedelta(days=15), idle) is True

        session.touch(NOW + timedelta(days=10))

        assert session.is_idle_at(NOW + timedelta(days=15), idle) is False

    def test_touching_does_not_extend_the_absolute_expiry(self) -> None:
        """The property that makes the 30-day bound absolute. A rotation
        that slid this would mean a chain refreshed daily never expires."""
        session = session_at()

        session.touch(NOW + timedelta(days=20))

        assert session.expires_at == NOW + THIRTY_DAYS

    def test_is_usable_at_checks_all_three_conditions(self) -> None:
        """The single question the refresh path asks, so that no caller can
        check two of the three and forget the third — which leaves revoked
        sessions working, silently."""
        idle = timedelta(days=14)

        assert session_at().is_usable_at(NOW, idle) is True
        assert session_at().is_usable_at(NOW + THIRTY_DAYS, idle) is False
        assert session_at().is_usable_at(NOW + timedelta(days=15), idle) is False

        revoked = session_at()
        revoked.revoke(at=NOW, reason=RevocationReason.PLAYER)
        assert revoked.is_usable_at(NOW, idle) is False


class TestSessionRevocation:
    def test_records_when_and_why(self) -> None:
        session = session_at()

        session.revoke(at=NOW, reason=RevocationReason.SUSPENSION)

        assert session.is_revoked is True
        assert session.revoked_at == NOW
        assert session.revoked_reason is RevocationReason.SUSPENSION

    def test_the_first_reason_wins(self) -> None:
        """A `reuse_detected` overwritten by a later `player` would erase
        the only record that an attack was found."""
        session = session_at()
        session.revoke(at=NOW, reason=RevocationReason.REUSE_DETECTED)

        session.revoke(at=NOW + timedelta(hours=1), reason=RevocationReason.PLAYER)

        assert session.revoked_reason is RevocationReason.REUSE_DETECTED
        assert session.revoked_at == NOW

    def test_re_revoking_does_not_raise(self) -> None:
        """A retried request must not error on the retry."""
        session = session_at()
        session.revoke(at=NOW, reason=RevocationReason.PLAYER)

        session.revoke(at=NOW, reason=RevocationReason.PLAYER)

        assert session.is_revoked is True


class TestSessionDevice:
    def test_every_field_is_optional(self) -> None:
        """A client that sends no `User-Agent` still gets a session.
        Refusing one because a cosmetic label is missing would turn a
        presentational gap into a sign-in failure."""
        session = session_at(device=None)

        assert session.device == SessionDevice()
        assert session.device.device_name is None

    def test_carries_what_se_2_asks_for(self) -> None:
        """SE-2: "without this the revocation list is a row of identical
        entries and the player cannot tell which one is the attacker"."""
        device = SessionDevice(
            device_name="Chrome on macOS",
            user_agent="Mozilla/5.0 ...",
            ip_address="203.0.113.7",
        )

        assert session_at(device=device).device == device

    def test_is_immutable(self) -> None:
        with pytest.raises(AttributeError):
            SessionDevice(device_name="a").device_name = "b"  # type: ignore[misc]
