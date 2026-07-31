"""The credential view `auth` needs to verify a sign-in — A64-011.2.

Separate from `dtos.py`, and the separation is load-bearing rather than
tidiness: everything in `dtos.py` is a Pydantic model, which means FastAPI
will happily accept it as a `response_model`, serialise it, and render it
into the OpenAPI schema. A type carrying a password hash must never be
one keystroke away from that. `UserCredentials` is a plain frozen
dataclass, so a route that named it as a response model would fail to
start rather than leak on the first request.

This is the second port `auth` needs from `users`, and `ports.py` predicted
it: "login will need [`password_hash`], and A64-011.2 should add a second,
equally narrow port for it rather than widening this one." It does.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.modules.users.public.dtos import UserRead


@dataclass(frozen=True, slots=True)
class UserCredentials:
    """Everything a sign-in decision needs about one account, and nothing
    else.

    Deliberately one object from one query rather than "look up the hash,
    then look up the account". Two queries would be two round trips on the
    hot path of the most-called endpoint on the platform, and — worse —
    they would take measurably longer for an address that exists than for
    one that doesn't, which is precisely the signal `auth` spends an
    Argon2 verification to hide.

    Frozen because a caller has no business editing a credential record in
    place; a rehash goes back through `UserCredentialStore`, which is the
    only writer.
    """

    account: UserRead
    """The public view of the account — exactly what the login endpoint
    returns once verification succeeds, which is why it is carried here
    rather than re-fetched afterwards."""

    password_hash: str = field(repr=False)
    """The stored Argon2id encoding, opaque to `users` and never returned
    on the wire.

    `repr=False` is not decoration. A dataclass repr is what ends up in a
    traceback frame, a `logger.debug("%s", creds)` call, and every error
    reporter that walks locals — services.md §8.5 keeps credential
    material out of all three. A hash is not a password, but it is offline
    -crackable material and is treated as a secret.
    """

    locked_until: datetime | None = None
    """When a temporary sign-in lock lapses; `None` when unlocked. The
    comparison against "now" belongs to the caller, which has the injected
    clock (AD-07) — this is data, not a decision."""
