"""`auth.domain.validators` — the password policy.

Pure functions, no database, no hashing. The one assertion that matters
most is at the bottom: no failure message ever contains the password.
"""

import pytest

from app.modules.auth.domain.exceptions import WeakPassword
from app.modules.auth.domain.validators import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    describe_password_policy,
    validate_password,
)

VALID = "CorrectHorse1!"


class TestAccepts:
    @pytest.mark.parametrize(
        "password",
        [
            "CorrectHorse1!",
            "Aa1!aaaa",  # exactly the minimum length
            "A" + "a" * 125 + "1!",  # exactly the maximum length
            "Пароль1!aB",  # non-ASCII is fine — a password is not a username
            "Aa1 !with spaces",  # whitespace is a legitimate character
            "Aa1!" + "🎲" * 4,  # emoji count as characters, not specials
        ],
    )
    def test_valid_passwords(self, password: str) -> None:
        assert validate_password(password) == password

    def test_returns_the_value_completely_unmodified(self) -> None:
        # Never trimmed, never case-folded. A password silently stripped at
        # registration would fail at login, and stripping also shrinks the
        # space an attacker must search.
        padded = "  Aa1!aaaa  "
        assert validate_password(padded) == padded


class TestRejects:
    @pytest.mark.parametrize(
        ("password", "reason"),
        [
            ("Aa1!aaa", "at least 8"),  # 7 chars
            ("", "at least 8"),
            ("A" + "a" * 126 + "1!", "at most 128"),  # 129 chars
            ("correcthorse1!", "uppercase"),
            ("CORRECTHORSE1!", "lowercase"),
            ("CorrectHorse!!", "digit"),
            ("CorrectHorse12", "special"),
        ],
    )
    def test_invalid_passwords(self, password: str, reason: str) -> None:
        with pytest.raises(WeakPassword, match=reason):
            validate_password(password)

    def test_carries_the_weak_password_code(self) -> None:
        with pytest.raises(WeakPassword) as exc_info:
            validate_password("short")
        assert exc_info.value.code == "weak_password"

    def test_length_is_checked_before_composition(self) -> None:
        # A 10 MB body must be rejected on length, not scanned character by
        # character four times first.
        with pytest.raises(WeakPassword, match="at most"):
            validate_password("a" * 10_000_000)


class TestNeverLeaksThePassword:
    @pytest.mark.parametrize(
        "password",
        [
            "hunter2secret",  # fails: no uppercase
            "HUNTER2SECRET",  # fails: no lowercase
            "HunterSecret!",  # fails: no digit
            "HunterSecret2",  # fails: no special
            "Hunt2!",  # fails: too short
            "S3cr3t!" + "x" * 200,  # fails: too long
        ],
    )
    def test_the_message_never_contains_the_password(self, password: str) -> None:
        """The single most important test in this file.

        An exception message is the most common way a credential reaches a
        log, a screenshot, or a bug tracker. Every rejection must describe
        the *rule*, never the value.
        """
        with pytest.raises(WeakPassword) as exc_info:
            validate_password(password)

        message = str(exc_info.value)
        assert password not in message
        # Nor any substantial fragment of it — a message quoting even a
        # prefix would be a partial disclosure.
        assert password[:6] not in message


class TestPolicyDescription:
    def test_lists_every_rule(self) -> None:
        lines = describe_password_policy()
        joined = " ".join(lines).lower()

        assert str(PASSWORD_MIN_LENGTH) in joined
        assert str(PASSWORD_MAX_LENGTH) in joined
        for requirement in ("uppercase", "lowercase", "digit", "special"):
            assert requirement in joined

    def test_is_derived_from_the_same_constants_the_validator_uses(self) -> None:
        # Guards against the description and the enforcement drifting —
        # the reason the helper exists rather than a hardcoded list in a
        # template.
        assert f"At least {PASSWORD_MIN_LENGTH} characters" in describe_password_policy()[0]
