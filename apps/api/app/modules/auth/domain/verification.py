"""The `EmailVerificationToken` entity — one issued verification link.

domain-model.md §6.1's account lifecycle has exactly one edge out of
`PendingVerification` into `Active`, labelled "email verified". This
entity is the credential that traverses it.

Framework-free by rule (architecture.md §8): no SQLAlchemy, no FastAPI,
no clock. Every method that needs "now" takes it as a parameter (AD-07),
which is what makes "this link expired an hour ago" a test that runs in
microseconds.

## Why an entity rather than a value object

It has identity that survives change: `used_at` is set once and the row
is still the same token. database.md §4.5 also requires it to be a *row*
rather than a self-contained signed value, and the reason is the whole
point of this design — a stateless signed token cannot be invalidated,
and one-time use is exactly the property that needs invalidation.

## The three ways a token stops working

    used         redeemed once, permanently — the one-time-use rule
    expired      the 24-hour window elapsed
    superseded   a newer token was issued and this one was invalidated

The third is not a separate flag. `invalidate_previous_tokens` marks
superseded tokens *used*, because "cannot be redeemed again" is exactly
what `used_at` means and a fourth column would be a second answer to the
same question. What distinguishes them is `used_at` versus a redemption
that also flipped `is_verified` — and that distinction lives in the
audit log, not in this row.

## Why the same shape will serve email *change*

The task requires supporting "future email change verification", and this
entity already does, with one addition it deliberately does not make yet:
a `new_email` column. Verifying a *change* means proving control of an
address the account does not yet have, so the target address must travel
with the token rather than being read from the account. That column has
no writer today, and an unused nullable column on a credential table
reads as wired-up — see `TokenType` in `domain/tokens.py` for the same
call. What matters now is that nothing here *prevents* it: the token is
keyed on `user_id` and carries no assumption that the address it proves
is the account's current one.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from app.core.identifiers import generate_uuid7


@dataclass(slots=True)
class EmailVerificationToken:
    """One issued verification link.

    Mutable in exactly one way — `used_at` is set once, by `consume`.

    **The raw token is not a field here and never will be.** Only
    `token_hash` is stored (database.md §4.5: "a database read — a backup,
    a replica, a support query — must not yield a working password
    reset", and the same applies here). An entity that could hold the
    plaintext is an entity that will eventually be logged with it.
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
    ) -> "EmailVerificationToken":
        """Builds a new, never-persisted token.

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
        """`instant >= expires_at`, not `>`. `expires_at` is when the link
        stops working, not the last instant it works — an off-by-one here
        is a credential that lives a second past its stated life, every
        time."""
        return instant >= self.expires_at

    def is_usable_at(self, instant: datetime) -> bool:
        """The single question the verify path asks.

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

        This does not verify anything. Flipping `is_verified` on the
        account is `users`' write, through a published port, and keeping
        the two apart is what stops this module from owning a column it
        does not own.
        """
        if self.is_used:
            return
        self.used_at = at
