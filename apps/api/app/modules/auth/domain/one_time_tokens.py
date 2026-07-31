"""`OneTimeToken` — the shape every stored, single-use credential on this
platform shares.

Extracted by A64-011.7, which needed an entity identical in every respect
to `EmailVerificationToken` except its name and its table. The extraction
follows the precedent `OpaqueTokenService` set in A64-011.6 and for the
same reason that module's docstring gives: database.md DB-24 names four
kinds of value with identical mechanics, and a second independently
written copy of "is this still redeemable" is how one of them ends up
comparing `>` where the other compares `>=`, or forgetting that a
superseded token is also a used one.

The three rules that live here are the ones that are silently wrong when
duplicated:

    expiry is `instant >= expires_at`   not `>` — see `is_expired_at`
    usable is *both* checks             not whichever the caller remembered
    consume keeps the first instant     a replay must not overwrite it

## Why subclasses rather than one class with a `kind` column

A verification token and a reset token are different credentials with
different lifetimes, different tables and — the load-bearing part —
different *powers*. One marks an address confirmed; the other replaces a
password and signs every device out. A single class distinguished by an
enum would make "redeem this reset token at the verification endpoint" a
runtime check somebody has to remember to write. Two types make it a type
error.

That is the same call `domain/tokens.py` records for `TokenType`, in the
opposite direction: JWT types are a closed enum because one *provider*
signs and verifies all of them, and the discriminator has to travel inside
the token. Here nothing travels — each token is looked up in its own
table by its own repository — so the discriminator can be the type itself.

Framework-free by rule (architecture.md §8): no SQLAlchemy, no FastAPI,
no clock. Every method that needs "now" takes it as a parameter (AD-07),
which is what makes "this link expired an hour ago" a test that runs in
microseconds.

## Why an entity rather than a value object

It has identity that survives change: `used_at` is set once and the row is
still the same token. database.md §4.5 also requires these to be *rows*
rather than self-contained signed values, and the reason is the whole
point of the design — a stateless signed token cannot be invalidated, and
one-time use is exactly the property that needs invalidation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Self
from uuid import UUID

from app.core.identifiers import generate_uuid7


@dataclass(slots=True)
class OneTimeToken:
    """One issued, hashed, single-use credential.

    Mutable in exactly one way — `used_at` is set once, by `consume`.

    **The raw token is not a field here and never will be.** Only
    `token_hash` is stored (database.md §4.5: "a database read — a backup,
    a replica, a support query — must not yield a working password
    reset"). An entity that could hold the plaintext is an entity that
    will eventually be logged with it.

    Not instantiated directly. Each concrete subclass names one kind of
    credential and one table; see this module's docstring.
    """

    id: UUID
    user_id: UUID
    """Opaque. This module never joins to it — DM-06's rule that an
    identifier is the only thing crossing a context boundary."""

    token_hash: bytes = field(repr=False)
    """SHA-256 of the token. `repr=False` because a dataclass repr lands
    in tracebacks and error reporters (services.md §8.5). A digest is not
    the token, but it is the value a stolen backup would be matched
    against, and it is treated as a secret."""

    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None

    @classmethod
    def issue(
        cls,
        *,
        user_id: UUID,
        token_hash: bytes,
        issued_at: datetime,
        lifetime: timedelta,
    ) -> Self:
        """Builds a new, never-persisted token of the calling subclass.

        Returns `Self`, not `OneTimeToken`, so
        `PasswordResetToken.issue(...)` is statically a
        `PasswordResetToken` — which is what stops the repositories from
        needing a cast and what makes the type distinction in this
        module's docstring actually enforceable.

        `issued_at` is a **parameter, not a `datetime.now()` call** —
        AD-07 forbids the domain from reading the clock, so the
        application layer, which holds the injected clock port, passes the
        instant in.

        `id` is generated here rather than by the database (DB-07): a
        UUIDv7 minted in Python is known before the insert.
        """
        return cls(
            id=generate_uuid7(),
            user_id=user_id,
            token_hash=token_hash,
            created_at=issued_at,
            expires_at=issued_at + lifetime,
        )

    # --- state ---------------------------------------------------------------

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    def is_expired_at(self, instant: datetime) -> bool:
        """`instant >= expires_at`, not `>`. `expires_at` is when the token
        stops working, not the last instant it works — an off-by-one here
        is a credential that lives a second past its stated life, every
        time."""
        return instant >= self.expires_at

    def is_usable_at(self, instant: datetime) -> bool:
        """The single question every redemption path asks.

        Exists so no caller can check one of the two conditions and forget
        the other — which is the failure that leaves used tokens
        redeemable, and it fails silently.
        """
        return not (self.is_used or self.is_expired_at(instant))

    # --- transitions ---------------------------------------------------------

    def consume(self, at: datetime) -> None:
        """Marks the token redeemed, permanently.

        **Idempotent, and the first consumption wins.** Re-consuming does
        not move `used_at`, because the instant a token was *first*
        redeemed is the fact worth keeping — a replay overwriting it would
        erase the only record of when the real redemption happened.

        This performs no effect beyond the token itself. Marking an
        address verified, or writing a new password hash, is `users`'
        write through a published port, and keeping the two apart is what
        stops this module from owning a column it does not own.
        """
        if self.is_used:
            return
        self.used_at = at
