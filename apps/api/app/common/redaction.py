"""What never reaches a log line — A64-028.6 §18, closing P2-2.

## Why this is at the boundary and not at the call sites

`CLAUDE.md` §8.3 requires redaction *at the logging boundary* "so redaction
cannot be forgotten". A64-028.1 found that it was at neither: `_JsonFormatter`
emitted whatever `extra={…}` a call site passed, and the only protection was
discipline. That discipline is currently good — `session_service` logs
identifiers and never the token — and it is exactly the kind of thing that
holds until the one call site that does not.

A filter cannot make a careless call site careful. What it can do is make
the careless call site **harmless**, which is the whole of the argument: a
token that reaches a logger is redacted rather than shipped to whatever
collects stdout, and the log still tells the operator that a field was
there.

## What it redacts, and how it decides

By **field name**, not by value inspection. A value-sniffing redactor is a
regex arms race that fails open on the first credential shaped differently
from the pattern; a name list fails *closed* on the field a call site
actually passed, which is the direction that matters.

The names are matched case-insensitively against a small set of substrings,
because the platform's call sites spell the same idea several ways
(`refresh_token`, `token`, `access_token`) and a list of exact names is a
list somebody has to keep in step.

## What it deliberately does not redact

`request_id`, `correlation_id`, `causation_id` and the domain identifiers
(`user_id`, `match_id`, …). Those are how an incident is reconstructed, and
`platform/metrics/__init__.py` already draws the line this platform chose:
identifiers are kept out of **metrics** and allowed in **logs**. Redacting
them would leave an operator with a log that says something happened to
somebody.

`email` **is** redacted. It is the one field that is both an identifier and
personal data, and the platform already has a rule about it — the email
verification service is tested to "never log the address".
"""

import logging
from typing import Any, Final

#: The marker a redacted value is replaced with. Kept as a value rather than
#: dropping the key: an operator needs to see that the field was present, and
#: a missing key reads as "the call site did not pass one".
REDACTED: Final = "[redacted]"

#: Substrings, matched case-insensitively against the field name.
#:
#: Every entry is a credential, a personal detail, or a whole request body
#: whose contents nobody has vetted. `secret` and `key` are broad on
#: purpose: a false positive costs an operator one field of context, and a
#: false negative ships a credential to a log aggregator.
SENSITIVE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "set_cookie",
        "token",
        "password",
        "secret",
        "api_key",
        "apikey",
        "credential",
        "otp",
        "vapid",
        "dsn",
        "email",
        "body",
        "payload",
    }
)

#: Names that contain a sensitive substring and are nevertheless safe.
#:
#: Without this, `token_family` — the identifier a reuse-detection incident
#: is reconstructed from, and not a credential — would be redacted, and
#: A64-028.2's whole rotation story would become unreadable in a log.
ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "token_family",
        "token_id",
        "has_token",
        "token_count",
        "payload_bytes",
    }
)


def is_sensitive(field: str) -> bool:
    name = field.lower()
    if name in ALLOWED_FIELDS:
        return False
    return any(marker in name for marker in SENSITIVE_FIELDS)


class RedactingFilter(logging.Filter):
    """Replaces sensitive `extra=` fields on the record itself.

    A filter rather than a formatter step, and attached to the **handler**
    like `_ContextFilter`, so it applies whichever formatter the environment
    chose — a redaction that only ran for JSON would leave every developer's
    local run unprotected, and local runs are where a token is most likely
    to be printed by hand while debugging.

    Mutates the record, which is the one thing a filter is allowed to do and
    is why this cannot be a formatter: by the time a formatter runs, a
    handler further along the chain may already have the original.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key.startswith("_") or not is_sensitive(key):
                continue
            record.__dict__[key] = REDACTED
        # A logging failure must never fail the request (`CLAUDE.md` §8.10),
        # so this returns True unconditionally: a record that could not be
        # redacted is still a record, and it has already been redacted by
        # the loop above whatever happened.
        return True


def redact(fields: dict[str, Any]) -> dict[str, Any]:
    """The same rule, for a caller assembling a dictionary by hand.

    Used by the places that log a structure rather than keyword fields —
    an error body, a configuration echo — where the boundary filter sees
    one opaque value and cannot look inside it.
    """
    return {key: (REDACTED if is_sensitive(key) else value) for key, value in fields.items()}


__all__ = [
    "ALLOWED_FIELDS",
    "REDACTED",
    "SENSITIVE_FIELDS",
    "RedactingFilter",
    "is_sensitive",
    "redact",
]
