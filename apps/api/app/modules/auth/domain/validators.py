"""The password policy — pure functions, no framework.

Same shape and same reasoning as `users/domain/validators.py`: one
definition of the rule, callable from a Pydantic `AfterValidator` at the
HTTP boundary, from the service for non-HTTP callers, and from a test —
because a Pydantic validator can only be reused by Pydantic.

**Nothing in this module ever puts the password in a message, a log, a
repr, or an exception.** Every failure describes the unmet *rule*. That is
the difference between an error a user can act on and a credential
disclosure.
"""

import string

from app.modules.auth.domain.exceptions import WeakPassword

PASSWORD_MIN_LENGTH = 8

# 128 is a security bound, not a form-field nicety. Argon2 hashes whatever
# it is given, and its cost is deliberately high — an unbounded password
# field is a trivially cheap way for one request to pin memory and CPU
# (`AuthSettings` sets ~19 MiB per hash). Rejecting long input *before*
# hashing is what keeps a public, unauthenticated endpoint from being an
# amplification primitive. bcrypt would additionally have needed this to
# avoid its 72-byte silent truncation; Argon2id has no such limit, so here
# the bound is purely about cost.
PASSWORD_MAX_LENGTH = 128

# Everything printable that is not a letter or a digit. Defined as a set
# rather than a regex character class so the "special character" rule
# needs no escaping decisions, and so a caller can see exactly which
# characters count.
SPECIAL_CHARACTERS = frozenset(string.punctuation)


def validate_password(value: str) -> str:
    """Returns the password unchanged if it meets the policy; raises
    `WeakPassword` otherwise.

    Returns the value **unmodified** — unlike an email, a password is
    never normalised, trimmed, or case-folded. Every byte the user typed
    is significant, and silently stripping whitespace would mean a
    password that works at registration and fails at login, or worse,
    quietly reduces the space an attacker has to search.

    Checks run longest-odds-first only incidentally; the order that
    matters is that **length is checked before anything else**, so a
    multi-megabyte body is rejected without scanning it.
    """
    if len(value) < PASSWORD_MIN_LENGTH:
        raise WeakPassword(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(value) > PASSWORD_MAX_LENGTH:
        raise WeakPassword(f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")

    if not any(character.isupper() for character in value):
        raise WeakPassword("Password must contain at least one uppercase letter.")
    if not any(character.islower() for character in value):
        raise WeakPassword("Password must contain at least one lowercase letter.")
    if not any(character.isdigit() for character in value):
        raise WeakPassword("Password must contain at least one digit.")
    if not any(character in SPECIAL_CHARACTERS for character in value):
        raise WeakPassword(
            "Password must contain at least one special character "
            f"({''.join(sorted(SPECIAL_CHARACTERS))})."
        )

    return value


def describe_password_policy() -> list[str]:
    """The policy as human-readable lines, for a client to render *before*
    a user submits.

    Exists so a sign-up form can show the requirements up front rather
    than discovering them one 422 at a time — and so that list comes from
    the same constants the validator enforces, instead of being retyped
    into a template where it would drift the first time the policy moves.
    """
    return [
        f"At least {PASSWORD_MIN_LENGTH} characters (at most {PASSWORD_MAX_LENGTH}).",
        "At least one uppercase letter.",
        "At least one lowercase letter.",
        "At least one digit.",
        "At least one special character.",
    ]
