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
    InvalidBio,
    InvalidCountryCode,
    InvalidDisplayName,
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


# --- shared text hygiene -----------------------------------------------------

#: Characters no player-authored text field on this platform may contain.
#: Newline and tab survive; every other C0/C1 control character does not.
#:
#: Not aesthetic. This text is rendered into a terminal by an admin tool,
#: into a log line by an abuse report, and into a web page by the client.
#: `\x1b` is an ANSI escape — enough to rewrite what a moderator sees in
#: their own terminal. `\u202e` (RIGHT-TO-LEFT OVERRIDE) reverses the
#: rendering of everything after it, which is a known display-spoofing
#: primitive.
#:
#: Stripping silently would be worse than refusing: the player would see
#: text they did not write. Both validators reject and name the character.
#:
#: Shared by `validate_bio` (A64-012.1) and `validate_display_name`
#: (A64-012.3) — one definition, because a display name shown in every
#: match list is a *better* place to hide an override than a biography
#: nobody scrolls to, and two copies is how one of them misses a codepoint.
_FORBIDDEN_TEXT_CHARACTERS = frozenset(
    chr(codepoint)
    for codepoint in [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0), 0x200B, 0x200E, 0x200F, 0x202A]
    + list(range(0x202B, 0x2030))
) - {"\n", "\t"}


# --- display name -----------------------------------------------------------

#: A64-012.3's figures. **Narrowed from A64-010's 1-64**, which was a bare
#: Pydantic bound with no domain rule behind it — the field is now editable,
#: so it needs one.
#:
#: The minimum is the interesting half. Three characters is what stops a
#: display name from being a single character or an invisible one, which is
#: the shape of an impersonation attempt: a one-character name renders as
#: near-nothing beside an avatar, and a player cannot tell two of them apart
#: in a match list. UP-1 makes the *handle* confusable-safe; this is the
#: much weaker guard on the free-form name shown next to it.
DISPLAY_NAME_MIN_LENGTH = 3
DISPLAY_NAME_MAX_LENGTH = 50


def validate_display_name(value: str) -> str:
    """Returns the display name trimmed, or raises `InvalidDisplayName`.

    **Unicode is fully supported**, deliberately and by requirement: the
    platform's own locales include Uzbek and Russian, and a rule that
    restricted this to ASCII would tell a large share of the player base
    that their own name is invalid. Length is therefore counted in
    *characters*, not bytes — `len()` on a `str` in Python 3 already does
    that, which is worth stating because the equivalent check in many
    languages does not, and "Жанибек" is 7 characters and 14 bytes.

    Trimmed rather than rejected for surrounding whitespace: a name pasted
    from another application routinely carries a trailing space, and
    refusing it teaches nothing. Trimming happens **before** the length
    check, so `"  a  "` is two characters and fails rather than passing on
    padding.

    Control and bidirectional characters are refused on exactly the same
    grounds as `validate_bio`, and by the same predicate — see
    `_FORBIDDEN_TEXT_CHARACTERS`. A display name is rendered beside an
    avatar in every match list and chat line on the platform, which makes
    it a *more* attractive place to hide a right-to-left override than a
    biography nobody scrolls to.

    Empty is not this function's concern: "no display name" is `None` at
    the field, and the caller normalises. Passing `""` here fails the
    minimum, which is the honest answer to "is this a valid name".
    """
    trimmed = value.strip()

    if len(trimmed) < DISPLAY_NAME_MIN_LENGTH:
        raise InvalidDisplayName(
            f"Display name must be at least {DISPLAY_NAME_MIN_LENGTH} characters."
        )
    if len(trimmed) > DISPLAY_NAME_MAX_LENGTH:
        raise InvalidDisplayName(
            f"Display name must be at most {DISPLAY_NAME_MAX_LENGTH} characters."
        )

    offending = sorted(_FORBIDDEN_TEXT_CHARACTERS.intersection(trimmed))
    if offending:
        raise InvalidDisplayName(
            "Display name must not contain control or bidirectional characters "
            f"(found U+{ord(offending[0]):04X})."
        )

    return trimmed


# --- other optional profile fields -------------------------------------------
#: A64-012.2 replaced the stored URL with an object key. Keys this
#: platform generates are ~60 characters (`avatars/{uuid}/{uuid}.webp`);
#: 512 is generous room for a provider prefix without letting the column
#: become somewhere arbitrary text can be parked.
AVATAR_OBJECT_KEY_MAX_LENGTH = 512


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


# --- biography (A64-012.1) --------------------------------------------------

#: A64-012.1's figure. Bounded for two independent reasons, and it is worth
#: separating them because only one is about product taste.
#:
#: The product reason is that a profile blurb is a blurb.
#:
#: The security reason is that this is the first free-text field on the
#: platform that one player writes and *other* players read. Every such
#: field is a storage-amplification surface (a row per account, unbounded)
#: and a rendering surface. The bound is the cheap half of the defence; the
#: character rules below are the other half.
BIO_MAX_LENGTH = 500


def validate_bio(value: str) -> str:
    """Returns the biography unchanged if it is acceptable plain text.

    **Plain text, and this module is where that means something.**
    A64-012.1 specifies that Markdown is not supported, which is a decision
    about *rendering* — but a field documented as plain text and stored
    without checking is one that a client will eventually render as
    something else. Nothing here escapes or transforms the value: escaping
    is the renderer's job and doing it at the boundary produces `&amp;amp;`
    the second time somebody escapes it again. What this does is keep the
    value *inert* — no control characters, no bidirectional overrides — so
    that a plain-text renderer is safe and a careless one is merely wrong.

    Trailing whitespace is stripped, because a bio that differs from
    another only by a trailing newline is the same bio, and leaving it
    makes the length bound depend on invisible characters.

    Empty is not an error and is not a value: it normalises to `None` at
    the *caller*, which is what makes "no bio" one state rather than two
    (`None` and `""`) that every renderer would have to check separately.
    """
    stripped = value.strip()

    if len(stripped) > BIO_MAX_LENGTH:
        raise InvalidBio(f"Bio must be at most {BIO_MAX_LENGTH} characters.")

    offending = sorted(_FORBIDDEN_TEXT_CHARACTERS.intersection(stripped))
    if offending:
        # Names the codepoint, never echoes the whole bio — an error
        # message is a place user-supplied text reaches logs and screens
        # (services.md §8.5), and this one is reachable by anyone.
        raise InvalidBio(
            f"Bio must not contain control or bidirectional characters "
            f"(found U+{ord(offending[0]):04X})."
        )

    return stripped


# --- country ----------------------------------------------------------------

#: ISO 3166-1 alpha-2. Two uppercase ASCII letters, stored as `char(2)`.
COUNTRY_CODE_LENGTH = 2

_COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")

#: Every officially assigned ISO 3166-1 alpha-2 code.
#:
#: A64-012.1 validated only the *shape* and said so plainly: `XX` and `ZZ`
#: passed, and the note recorded that membership belongs in the
#: `reference.country` table database.md §201 specifies. A64-012.3 makes
#: the field editable by anyone with an account and requires unknown codes
#: to be rejected, so the check can no longer wait for that table.
#:
#: **A frozen set here rather than a dependency**, per CLAUDE.md §2.6 ("do
#: not add a dependency for what the standard library or an existing
#: dependency does"): `pycountry` would pull the whole ISO corpus —
#: subdivisions, historic codes, translations — to answer one membership
#: question, and would make a data update a release.
#:
#: The cost is honest and worth naming: this list ages. ISO assigns and
#: retires codes a handful of times a decade (`SS` in 2011, `XK` still
#: unassigned for Kosovo), so a country that appears next year needs a
#: code change here. That is the trade `reference.country` eventually
#: fixes — this set moves into a seeded table, `validate_country_code`
#: takes the set as an argument, and operations correct it without a
#: deploy. Until then a stale entry fails *closed*, which is the safe
#: direction: a player from a brand-new country cannot set their flag,
#: rather than an arbitrary two letters being stored as if valid.
#:
#: Deliberately excludes user-assigned ranges (`AA`, `QM`-`QZ`, `XA`-`XZ`,
#: `ZZ`) and exceptional reservations. Those are *legal* ISO values for
#: private use, and accepting them would put arbitrary two-letter strings
#: back in the column by the front door.
ISO_3166_1_ALPHA_2: frozenset[str] = frozenset(
    [
        "AD",
        "AE",
        "AF",
        "AG",
        "AI",
        "AL",
        "AM",
        "AO",
        "AQ",
        "AR",
        "AS",
        "AT",
        "AU",
        "AW",
        "AX",
        "AZ",
        "BA",
        "BB",
        "BD",
        "BE",
        "BF",
        "BG",
        "BH",
        "BI",
        "BJ",
        "BL",
        "BM",
        "BN",
        "BO",
        "BQ",
        "BR",
        "BS",
        "BT",
        "BV",
        "BW",
        "BY",
        "BZ",
        "CA",
        "CC",
        "CD",
        "CF",
        "CG",
        "CH",
        "CI",
        "CK",
        "CL",
        "CM",
        "CN",
        "CO",
        "CR",
        "CU",
        "CV",
        "CW",
        "CX",
        "CY",
        "CZ",
        "DE",
        "DJ",
        "DK",
        "DM",
        "DO",
        "DZ",
        "EC",
        "EE",
        "EG",
        "EH",
        "ER",
        "ES",
        "ET",
        "FI",
        "FJ",
        "FK",
        "FM",
        "FO",
        "FR",
        "GA",
        "GB",
        "GD",
        "GE",
        "GF",
        "GG",
        "GH",
        "GI",
        "GL",
        "GM",
        "GN",
        "GP",
        "GQ",
        "GR",
        "GS",
        "GT",
        "GU",
        "GW",
        "GY",
        "HK",
        "HM",
        "HN",
        "HR",
        "HT",
        "HU",
        "ID",
        "IE",
        "IL",
        "IM",
        "IN",
        "IO",
        "IQ",
        "IR",
        "IS",
        "IT",
        "JE",
        "JM",
        "JO",
        "JP",
        "KE",
        "KG",
        "KH",
        "KI",
        "KM",
        "KN",
        "KP",
        "KR",
        "KW",
        "KY",
        "KZ",
        "LA",
        "LB",
        "LC",
        "LI",
        "LK",
        "LR",
        "LS",
        "LT",
        "LU",
        "LV",
        "LY",
        "MA",
        "MC",
        "MD",
        "ME",
        "MF",
        "MG",
        "MH",
        "MK",
        "ML",
        "MM",
        "MN",
        "MO",
        "MP",
        "MQ",
        "MR",
        "MS",
        "MT",
        "MU",
        "MV",
        "MW",
        "MX",
        "MY",
        "MZ",
        "NA",
        "NC",
        "NE",
        "NF",
        "NG",
        "NI",
        "NL",
        "NO",
        "NP",
        "NR",
        "NU",
        "NZ",
        "OM",
        "PA",
        "PE",
        "PF",
        "PG",
        "PH",
        "PK",
        "PL",
        "PM",
        "PN",
        "PR",
        "PS",
        "PT",
        "PW",
        "PY",
        "QA",
        "RE",
        "RO",
        "RS",
        "RU",
        "RW",
        "SA",
        "SB",
        "SC",
        "SD",
        "SE",
        "SG",
        "SH",
        "SI",
        "SJ",
        "SK",
        "SL",
        "SM",
        "SN",
        "SO",
        "SR",
        "SS",
        "ST",
        "SV",
        "SX",
        "SY",
        "SZ",
        "TC",
        "TD",
        "TF",
        "TG",
        "TH",
        "TJ",
        "TK",
        "TL",
        "TM",
        "TN",
        "TO",
        "TR",
        "TT",
        "TV",
        "TW",
        "TZ",
        "UA",
        "UG",
        "UM",
        "US",
        "UY",
        "UZ",
        "VA",
        "VC",
        "VE",
        "VG",
        "VI",
        "VN",
        "VU",
        "WF",
        "WS",
        "YE",
        "YT",
        "ZA",
        "ZM",
        "ZW",
    ]
)


def validate_country_code(value: str) -> str:
    """Returns an upper-cased ISO 3166-1 alpha-2 code, or raises
    `InvalidCountryCode`.

    Two checks, and the second is what A64-012.3 added: the value must be
    two ASCII letters **and** must be a code ISO has actually assigned.
    `XX`, `ZZ` and `QQ` are all well-formed and all rejected — see
    `ISO_3166_1_ALPHA_2` on why the private-use ranges are excluded rather
    than tolerated.

    Upper-casing rather than rejecting lowercase: `gb` and `GB` are the
    same country, and a form that rejected the first would be rejecting a
    keyboard rather than a value.

    The error names one valid example and never enumerates the set. A
    249-entry list in a 422 body is unreadable, and the client rendering
    this field has its own country picker — the message is for a developer
    who sent something odd, not a menu.
    """
    normalised = value.strip().upper()

    if not _COUNTRY_CODE_PATTERN.match(normalised):
        raise InvalidCountryCode(
            f"Country must be a two-letter ISO 3166-1 alpha-2 code such as 'GB'; got {value!r}."
        )

    if normalised not in ISO_3166_1_ALPHA_2:
        raise InvalidCountryCode(
            f"{normalised!r} is not an assigned ISO 3166-1 alpha-2 country code."
        )

    return normalised
