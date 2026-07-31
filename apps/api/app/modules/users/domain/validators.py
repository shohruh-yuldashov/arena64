"""Reusable field validation — pure functions, no framework.

This module is the single definition of what a valid username, email,
language or timezone *is*. Everything else calls into it:

  `domain/value_objects.py`     constructs value objects from raw strings
  `presentation/schemas/`       Pydantic `AfterValidator`s wrap these directly
  `application/services/`       re-validates values arriving from non-HTTP
                                callers (a future Celery task, an admin tool)

That single-definition property is the whole point of the task's
"validation should be reusable" requirement, and it is why these are plain
functions rather than Pydantic validators: a Pydantic validator can only
be reused by Pydantic. A pure function can be called from a domain
constructor that architecture.md §8 forbids from importing a framework at
all.

Each raises a typed domain error from `domain/exceptions`, never a bare
`ValueError` — services.md §7 requires callers to branch on error *type*,
and `presentation/` maps those types to HTTP statuses through the existing
handler table without a single `except ValueError` anywhere.
"""

import re
import unicodedata
from functools import lru_cache
from zoneinfo import available_timezones

from app.core.enums import Locale
from app.modules.users.domain.exceptions import (
    InvalidEmail,
    InvalidLanguage,
    InvalidTimezone,
    InvalidUsername,
)

# --- username ---------------------------------------------------------------

USERNAME_MIN_LENGTH = 3
# 20, tightened from A64-010's 32 by A64-011.1's explicit specification.
# The rule changed here rather than only in the registration schema on
# purpose: a username created through `POST /auth/register` and one
# created through any other path must be the same kind of thing, and two
# policies for one concept is exactly the divergence CLAUDE.md §2.1 warns
# about. The database's own CHECK constraint moves with it (migration
# `3caf68aa8cfc`), so the authoritative guard and the domain agree.
USERNAME_MAX_LENGTH = 20

# Starts alphanumeric, then alphanumerics or underscore. Leading
# punctuation is excluded because "_admin" reads as `admin` at a glance in
# most UI fonts — the same impersonation surface UP-1 is about.
#
# The hyphen A64-010 allowed is gone, per A64-011.1's specification
# ("letters, numbers, underscore"). Dropping a permitted character is the
# safe direction for a change like this: it can only invalidate names that
# do not exist yet, whereas *adding* one later is free.
#
# **ASCII-only, and that is a deliberate interim restriction.**
# domain-model.md §14.6 anticipates non-ASCII handles and names the exact
# hazard: Arena64 serves English, Russian and Uzbek speakers, so Cyrillic
# homoglyphs (`о`, `а`, `е`, `р`, `с` against their Latin twins) are a
# routine impersonation vector, not an exotic one. The defence it
# specifies is a confusable *skeleton* column, which needs a Unicode
# confusables table this platform has not adopted yet (database.md RK-3
# tracks it). Until that exists, allowing Cyrillic handles would open the
# vector with nothing defending it — so the restriction stands, and
# lifting it is gated on implementing the skeleton, not on someone
# deciding the regex looks unfriendly.
_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_]*$")

# Names that must never belong to a player because the platform itself uses
# them, or because holding one lets somebody impersonate the platform in a
# conversation (domain-model.md UP-1). Compared against the *folded* form,
# so "Admin" and "ADMIN" are caught too.
RESERVED_USERNAMES: frozenset[str] = frozenset(
    {
        "admin",
        "administrator",
        "root",
        "system",
        "arena64",
        "support",
        "help",
        "moderator",
        "mod",
        "staff",
        "official",
        "api",
        "www",
        "me",
        "null",
        "undefined",
        "anonymous",
        "deleted",
    }
)


def fold_username(value: str) -> str:
    """The case-insensitive comparison form — domain-model.md UP-1's
    "unique case-insensitively".

    NFKC first (collapses compatibility variants: fullwidth `ａｄｍｉｎ`
    -> `admin`), then `.lower()`.

    **`.lower()` and not `.casefold()`, deliberately.** Casefold is the
    stronger Unicode folding and would be the better choice in isolation —
    but this function is not in isolation. The database enforces the
    uniqueness rule through a generated column computing
    `lower(normalize(username, NFKC))`, and PostgreSQL's `lower()` is not
    Python's `casefold()`: verified empirically, `'Straße'` casefolds to
    `'strasse'` but PostgreSQL lowers it to `'straße'`.

    If the two disagreed, they would disagree in the worst possible way —
    the service's `exists_by_username` pre-check and the constraint that
    actually enforces uniqueness would reach *opposite* verdicts on the
    same pair of names, so a sign-up could be rejected by the pre-check
    that the database would have allowed, or pass the pre-check and then
    violate a constraint the caller was told nothing about. BE-06 settles
    which side must move: the constraint is the authoritative check, so
    Python matches PostgreSQL rather than the other way round.

    The cost is that `STRASSE` and `Straße` are treated as distinct
    usernames. That is a marginal impersonation gap in one language, and
    it is not the layer meant to close impersonation anyway — the
    confusable-skeleton layer (database.md §14.6, deferred; see
    `infrastructure/models.py`) is.
    """
    return unicodedata.normalize("NFKC", value).lower()


def validate_username(value: str) -> str:
    """Returns the username unchanged if valid; raises `InvalidUsername`
    otherwise. Does not fold — the stored value keeps the capitalisation
    the player chose, and only the *comparison* form is folded.
    """
    if not value:
        raise InvalidUsername("Username must not be empty.")

    if len(value) < USERNAME_MIN_LENGTH:
        raise InvalidUsername(f"Username must be at least {USERNAME_MIN_LENGTH} characters.")
    if len(value) > USERNAME_MAX_LENGTH:
        raise InvalidUsername(f"Username must be at most {USERNAME_MAX_LENGTH} characters.")

    if not _USERNAME_PATTERN.match(value):
        raise InvalidUsername(
            "Username must start with a letter or digit and contain only "
            "letters, digits and underscores."
        )

    if fold_username(value) in RESERVED_USERNAMES:
        # Deliberately the same message as any other invalid username: a
        # distinct "that name is reserved" would confirm which names the
        # platform considers special, which is free reconnaissance.
        raise InvalidUsername("That username is not available.")

    return value


# --- email ------------------------------------------------------------------

EMAIL_MAX_LENGTH = 254  # RFC 5321 §4.5.3.1.3 — the whole reverse-path

# Deliberately permissive-but-structural, not an RFC 5322 parser. A regex
# that fully implements 5322 is famously unreadable and still cannot tell
# you whether an address *receives mail* — only a verification email can,
# and `is_verified` exists for exactly that. This rejects the shapes that
# are certainly wrong (no `@`, no dot in the domain, whitespace) and lets
# delivery decide the rest.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    """The stored and compared form — trimmed, NFKC-normalised, lowered.

    Unlike a username, an email has no display value worth preserving
    separately: nobody renders `Player@Example.COM` back to its owner in
    that exact casing, and keeping two forms would mean two columns and a
    way for them to disagree. domain-model.md AC-1 requires
    case-insensitive uniqueness; normalising on the way in gets it with
    one column and one plain unique constraint.

    `.lower()` rather than `.casefold()` for the same reason as
    `fold_username` — see its docstring. Here the stakes are lower (the
    stored value *is* the normalised one, so there is no second authority
    to disagree with), but using two different folding rules in one file
    for no reason would be worse than the marginal folding strength is
    worth.
    """
    return unicodedata.normalize("NFKC", value.strip()).lower()


def validate_email(value: str) -> str:
    """Returns the *normalised* email if valid; raises `InvalidEmail`
    otherwise. Returns the normalised form rather than the input because
    an un-normalised email is never the value that should be stored.
    """
    normalized = normalize_email(value)

    if not normalized:
        raise InvalidEmail("Email must not be empty.")
    if len(normalized) > EMAIL_MAX_LENGTH:
        raise InvalidEmail(f"Email must be at most {EMAIL_MAX_LENGTH} characters.")
    if not _EMAIL_PATTERN.match(normalized):
        raise InvalidEmail("Email is not a valid address.")

    return normalized


# --- optional profile fields ------------------------------------------------
# No validator function: unlike the four fields above, these have no rule
# beyond a length bound, and a `validate_display_name` that only checked
# `len()` would be indirection with nothing in it. The constants are still
# defined here — and only here — so the ORM column, the Pydantic schema and
# any future check all read the same number instead of three literals that
# drift apart (CLAUDE.md §2.1, one source of truth per concept).
DISPLAY_NAME_MAX_LENGTH = 64
AVATAR_URL_MAX_LENGTH = 2048  # the de-facto maximum URL length browsers accept


# --- preferred language -----------------------------------------------------


def validate_language(value: str) -> Locale:
    """Coerces to the platform's supported-locale enum (`app.core.enums`,
    A64-008) — `en`, `ru`, `uz`. Deliberately not a second list of language
    codes: domain-model.md keys ratings and content by the same three, and
    a module-local copy would drift the first time a fourth is added.
    """
    try:
        return Locale(value)
    except ValueError as exc:
        supported = ", ".join(locale.value for locale in Locale)
        raise InvalidLanguage(
            f"Unsupported language {value!r}. Supported languages: {supported}."
        ) from exc


# --- timezone ---------------------------------------------------------------


@lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    """The IANA names this system has, computed once per process.

    `zoneinfo.available_timezones()` walks the whole tzdata tree and
    rebuilds a ~600-element set on **every** call — measured at **10.4ms**
    on this platform's development machine.

    That was written off in A64-010 as acceptable because validation "is
    only ever hit on a write path... never per-render". The assumption was
    wrong, and A64-011.2 is where it became a security bug rather than a
    performance one. `SqlAlchemyUserRepository._to_domain` constructs a
    `Timezone` for every row it maps, so the cost lands on every *read*
    too — which made a login for an existing address take 11.5ms longer
    than one for an address that does not exist. That is precisely the
    account-enumeration oracle `AuthenticationService`'s dummy hash exists
    to close, reopened one layer down and much larger than the Argon2
    noise floor: the two verifications agree to within 1%, and this
    disagreed by 33×.

    Caching means a tzdata package updated underneath a running process is
    not picked up until it restarts. That is the correct trade: the set
    changes a handful of times a year, deployments are more frequent than
    that, and an unrecognised-but-real timezone is a 422 rather than a
    correctness failure.
    """
    return frozenset(available_timezones())


def validate_timezone(value: str) -> str:
    """Validates against the IANA database the running system actually has,
    via `zoneinfo` — not a hardcoded list, which would go stale every time
    a country changes its rules (and they do, several times a year).

    Stored as the IANA name (`Europe/London`), never as a UTC offset:
    an offset is a *fact about one instant*, not a timezone, and storing
    one would silently break every player the moment their region's
    daylight-saving rules applied. That is the same reasoning as
    domain-model.md DM-14's rule for instants, applied to the preference
    that renders them.
    """
    if not value:
        raise InvalidTimezone("Timezone must not be empty.")

    if value not in _known_timezones():
        raise InvalidTimezone(
            f"Unknown timezone {value!r}. Expected an IANA name such as 'Europe/London'."
        )

    return value
