"""The `auth` ORM model — the first table this module owns.

A64-011.1's `infrastructure/__init__.py` noted that `auth` "stores nothing
of its own" and had no model. A64-011.4 is where that changes: a refresh
session is `auth`'s state, not `users`', and it lives in `auth`'s own
schema per database.md §3.1 — "the most sensitive schema on the platform".

## Two documented deviations

**1. The foreign key crosses a schema boundary.** DB-03 forbids
referential integrity between module schemas, and the reasoning is real:
a cross-schema FK turns architecture.md §16's extraction stages into a
rewrite rather than an adapter swap. The task specifies "Use foreign
keys", and here the task is also the better call, for two reasons
specific to this table:

  - The boundary this FK crosses is *already* collapsed. A64-010
    deliberately merged `auth.account` into `users.user` (see that
    module's docstring), so the account row this session belongs to is in
    `users` by an earlier, documented decision. The FK does not create an
    extraction problem; it points at one that already exists and is
    already recorded.
  - A session row that outlives its account is a live credential with no
    owner. `ON DELETE CASCADE` is the difference between erasing a user
    and erasing a user's ability to authenticate. The reconciliation job
    DB-03 offers as the alternative (§17 R-9) detects orphans *later* —
    and "later" on a credential table means a window in which a deleted
    account can still refresh.

When `auth.account` is eventually split out per DM-10, this FK moves to
point at it and stops crossing anything.

**2. The table name is plural.** database.md §2.2 mandates singular
(`match`, `friend_request`), and `users.user` follows it. The task names
this table `user_sessions`, and an explicit instruction outranks a
convention — but it does make this the one plural table in the schema.
Renaming it is a one-line migration if the convention is preferred.

## What is deliberately absent

No `refresh_token` column of any kind. Only the SHA-256 hash is stored
(§14.3: "the token itself exists only in transit and in the client"), and
there is no column here that could hold a recoverable form of it.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins import UUIDPrimaryKeyMixin
from app.database.types import IpAddress, UtcDateTime
from app.modules.auth.domain.sessions import RevocationReason
from app.modules.users.infrastructure.models import USERS_SCHEMA

AUTH_SCHEMA = "auth"

#: Bounded so a hostile client cannot use the header as free storage.
#: Real user agents are well under 200 characters; 512 is generous enough
#: that nothing legitimate is truncated and small enough that a million
#: sessions cannot become a hundred megabytes of someone else's data.
USER_AGENT_MAX_LENGTH = 512
DEVICE_NAME_MAX_LENGTH = 120

#: SHA-256 output. Fixed width, so the column can say so rather than
#: accepting anything — a 16-byte value in this column would mean
#: something other than this platform wrote the row.
REFRESH_TOKEN_HASH_LENGTH = 32

#: SHA-256 output, as above. Same algorithm, same width, different table.
VERIFICATION_TOKEN_HASH_LENGTH = 32

#: SHA-256 output, as above. A third named constant rather than one shared
#: `TOKEN_HASH_LENGTH`, because these are three independent decisions that
#: merely agree today: a table whose digest algorithm changed would need
#: its own width, and a shared constant would make that a migration
#: touching tables that were not changing.
PASSWORD_RESET_TOKEN_HASH_LENGTH = 32


class UserSessionModel(Base, UUIDPrimaryKeyMixin):
    """The `auth.user_sessions` row.

    Composes `UUIDPrimaryKeyMixin` (application-generated UUIDv7, DB-07)
    but deliberately **not** `TimestampMixin`: that mixin provides
    `created_at`/`updated_at`, and this table's mutable timestamp is
    `last_used_at`, which means something specific and is not "when the
    row was touched". An `updated_at` alongside it would be a second,
    subtly different answer to the same question — and the one that drifts
    is always the redundant one.

    Also deliberately not `SoftDeleteMixin`: DB-20 forbids a generic
    `deleted_at`, and `revoked_at` is the named domain state that actually
    applies. A revoked session is not deleted — it is evidence, and SE-2's
    revocation list needs it.
    """

    __tablename__ = "user_sessions"

    __table_args__ = (
        # The lookup every refresh performs, and the constraint that makes
        # a token identify at most one session. Unique rather than merely
        # indexed: two rows sharing a hash would mean one presented token
        # matching two sessions, and no correct behaviour exists for that.
        Index("uq_user_sessions__refresh_token_hash", "refresh_token_hash", unique=True),
        # Serves `list_user_sessions` and `revoke_all_sessions`. Ordered
        # `(user_id, revoked_at)` because both queries filter on the user
        # first and then care about liveness; `created_at` is included so
        # the listing's ordering is served by the same index rather than
        # by a sort.
        Index("ix_user_sessions__user_id_revoked_at", "user_id", "revoked_at", "created_at"),
        # Reuse detection revokes by family (§14.3), so that revocation
        # must not be a sequential scan of every session on the platform
        # at the exact moment an attack is being contained.
        Index("ix_user_sessions__token_family", "token_family"),
        # §4.4's constraint, verbatim: "`revoked_at` is set if and only if
        # `revoked_reason` is set." Enforced by the database because BE-06
        # makes the database authoritative — a half-revoked row, revoked
        # with no reason or reasoned with no revocation, is a row no
        # application logic knows how to read.
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="revocation_is_complete",
        ),
        # An absolute expiry before the session existed is nonsense that
        # would make every read of the row wrong; cheap to forbid outright.
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            f"octet_length(refresh_token_hash) = {REFRESH_TOKEN_HASH_LENGTH}",
            name="refresh_token_hash_length",
        ),
        {"schema": AUTH_SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        # Crosses into `users` — see this module's docstring for why that
        # is a deliberate, argued deviation from DB-03 rather than an
        # oversight. `CASCADE` because a session that outlives its account
        # is a credential with no owner.
        ForeignKey(f"{USERS_SCHEMA}.user.id", ondelete="CASCADE"),
        nullable=False,
    )

    refresh_token_hash: Mapped[bytes] = mapped_column(
        # `bytea`, not `text`. A hash is bytes; storing the hex rendering
        # would double the storage and invite a comparison against the
        # wrong encoding of the same value — the classic "why does this
        # never match" bug.
        LargeBinary(REFRESH_TOKEN_HASH_LENGTH),
        nullable=False,
    )

    token_family: Mapped[uuid.UUID] = mapped_column(nullable=False)
    """database.md §14.3's `chain_id`. Deliberately *not* a self-referencing
    foreign key to `id`: the family root may be revoked and swept while
    descendants are still live, and a FK would either block that or
    cascade the deletion of sessions that are still in use."""

    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    device_name: Mapped[str | None] = mapped_column(String(DEVICE_NAME_MAX_LENGTH), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(USER_AGENT_MAX_LENGTH), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(
        # PostgreSQL's `inet`, not `varchar`: it validates the value, it
        # stores IPv6 without a 45-character column, and it makes subnet
        # queries possible for the anomaly detection SE-2 exists to enable.
        # A malformed address is rejected by the database rather than
        # discovered later by whatever tries to parse it.
        #
        # Wrapped in `IpAddress` so the value comes back as `str` rather
        # than asyncpg's `IPv4Address` — see that type on why the
        # annotation above would otherwise be a lie.
        IpAddress,
        nullable=True,
    )

    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_reason: Mapped[RevocationReason | None] = mapped_column(
        # A native enum per DB-15: closed, stable, and on a table that will
        # hold several rows per player. `values_callable` stores the member
        # *values* ("player"/"reuse_detected"), not the Python member
        # *names*, which is what every other system on the platform speaks.
        PgEnum(
            RevocationReason,
            name="session_revoke_reason",
            schema=AUTH_SCHEMA,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=True,
    )

    # Explicit re-declarations so the reader sees the full row shape here
    # rather than having to open a mixin. Types match exactly.
    id: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    def __repr__(self) -> str:
        # Never includes the hash, the user agent or the IP — a repr lands
        # in logs and tracebacks, and two of those three are Personal data
        # under database.md §14.1 (services.md §8.5).
        return f"<UserSessionModel id={self.id!r} user_id={self.user_id!r}>"


class EmailVerificationTokenModel(Base, UUIDPrimaryKeyMixin):
    """The `auth.email_verification_tokens` row — database.md §4.5.

    Deliberately **not** composing `TimestampMixin`: that supplies
    `updated_at`, and this row has exactly one mutation (`used_at`) whose
    meaning is specific. A generic "row was touched" alongside it would be
    a second, subtly different answer to the same question.

    The foreign key crosses into `users` for the reasons argued at length
    on `UserSessionModel` — the boundary is already collapsed by A64-010's
    documented merge, and `ON DELETE CASCADE` is what stops a deleted
    account from leaving a live credential behind.

    **These rows are hard-deleted on a schedule** (§4.5): "a consumed
    reset token has no evidentiary value and retaining it is pure
    liability". No such job exists yet — it is a recommendation for
    A64-011.7, which adds the structurally identical
    `password_reset_token` and should build one sweeper for both.
    """

    __tablename__ = "email_verification_tokens"

    __table_args__ = (
        # The lookup every redemption performs, and the constraint that
        # makes a token identify at most one row. Unique rather than
        # merely indexed: two rows sharing a digest would mean one link
        # matching two tokens, and no correct behaviour exists for that.
        Index("uq_email_verification_tokens__token_hash", "token_hash", unique=True),
        # Serves `invalidate_active_for_user` and the resend path.
        Index("ix_email_verification_tokens__user_id", "user_id"),
        # Serves the future sweeper. Indexed now because adding an index
        # to a table with millions of rows is a maintenance window, and
        # adding it to an empty one is free.
        Index("ix_email_verification_tokens__expires_at", "expires_at"),
        # §4.5: "a partial unique index on `account_id` covering only rows
        # where `consumed_at` is null ... keeps at most one live token per
        # account."
        #
        # Enforced by the database rather than by the service, because
        # BE-06 makes the database authoritative: two concurrent resends
        # both pass a check-then-act, and only this index is correct under
        # concurrency. It is what turns "invalidate the previous token"
        # from an intention into a guarantee.
        #
        # Expiry is deliberately *not* in the predicate, though the doc
        # mentions it: `now()` is not immutable, so PostgreSQL rejects it
        # in an index predicate outright. The service expires rows by
        # marking them used, which this index does see.
        Index(
            "uq_email_verification_tokens__one_live_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("used_at IS NULL"),
        ),
        CheckConstraint(
            f"octet_length(token_hash) = {VERIFICATION_TOKEN_HASH_LENGTH}",
            name="token_hash_length",
        ),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        {"schema": AUTH_SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{USERS_SCHEMA}.user.id", ondelete="CASCADE"),
        nullable=False,
    )

    token_hash: Mapped[bytes] = mapped_column(
        LargeBinary(VERIFICATION_TOKEN_HASH_LENGTH), nullable=False
    )

    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """Set once, on redemption *or* on being superseded by a newer token.
    Both mean "cannot be redeemed again", which is exactly what this
    column says — see the entity's `consume` docstring on why there is no
    fourth state."""

    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'link'"))
    """`domain.verification.VerificationChallengeKind` — A64-021.5H §4.

    Text rather than a PostgreSQL enum, like every other closed vocabulary
    on this platform: adding a challenge kind must be a code change and a
    migration of rows, never an `ALTER TYPE` that locks a credential table.

    `server_default` so the migration backfills every existing row as a
    link without a second statement — which is what they are, and what
    keeps already-issued links working (§13)."""

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """Failed guesses against a code.

    **Incremented by the database, never read-then-written.** A
    check-then-act would let two concurrent submissions each read four and
    each write five, giving an attacker a sixth guess — the same argument
    the partial unique index above makes about concurrent resends, applied
    to the one column an attacker can move.

    Always `0` for a link: guessing a 32-byte random value is not a threat
    model, so nothing increments it."""

    id: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    def __repr__(self) -> str:
        # Never the hash — a repr lands in logs and tracebacks.
        return f"<EmailVerificationTokenModel id={self.id!r} user_id={self.user_id!r}>"


class PasswordResetTokenModel(Base, UUIDPrimaryKeyMixin):
    """The `auth.password_reset_tokens` row — database.md §4.5.

    Structurally identical to `EmailVerificationTokenModel`, which §4.5
    says outright ("Structurally identical: `id`, `account_id` (FK,
    cascade), `token_hash bytea`, `expires_at`, `consumed_at` ...
    `created_at`"), and the two are nonetheless declared separately rather
    than sharing a mixin.

    **Why the columns are repeated instead of factored out.** The
    *behaviour* these two credentials share is real and is shared —
    `OneTimeToken` holds it, and A64-011.7 extracted it precisely so it
    would not be written twice. A table declaration is not behaviour. It is
    a statement of what one relation looks like, and the two tables agree
    today by coincidence of requirements rather than by rule: the reset
    table is a plausible place for §4.5's `requested_ip`, the verification
    table is a plausible place for a `new_email` (see
    `domain/verification.py`), and a shared declarative mixin would make
    either addition a change to both. Factoring here would buy roughly
    fifteen lines and would couple two tables whose whole future is to
    diverge.

    Deliberately **not** composing `TimestampMixin`: that supplies
    `updated_at`, and this row has exactly one mutation (`used_at`) whose
    meaning is specific. A generic "row was touched" alongside it would be
    a second, subtly different answer to the same question.

    The foreign key crosses into `users` for the reasons argued at length
    on `UserSessionModel` — the boundary is already collapsed by A64-010's
    documented merge, and `ON DELETE CASCADE` is what stops a deleted
    account from leaving behind a live credential that replaces passwords.

    **These rows are hard-deleted on a schedule** (§4.5): "a consumed
    reset token has no evidentiary value and retaining it is pure
    liability". No such job exists yet. A64-011.6 recommended that
    A64-011.7 build one sweeper covering both tables; A64-011.7 did not,
    because a scheduled job needs a scheduler this platform has not chosen
    yet (architecture.md AD-02's worker profile is a deployment target, not
    a running clock). It is the first recommendation for A64-011.8, and
    `ix_password_reset_tokens__expires_at` below is the index it will use.
    """

    __tablename__ = "password_reset_tokens"

    __table_args__ = (
        # The lookup every redemption performs, and the constraint that
        # makes a token identify at most one row. Unique rather than merely
        # indexed: two rows sharing a digest would mean one link matching
        # two tokens, and there is no correct behaviour for that when the
        # thing being matched decides whose password gets replaced.
        Index("uq_password_reset_tokens__token_hash", "token_hash", unique=True),
        # Serves `invalidate_active_for_user`, which runs on every forgot
        # request and again on every successful reset.
        Index("ix_password_reset_tokens__user_id", "user_id"),
        # Serves the sweeper described above. Indexed now because adding an
        # index to a table with millions of rows is a maintenance window,
        # and adding it to an empty one is free.
        Index("ix_password_reset_tokens__expires_at", "expires_at"),
        # §4.5: "a partial unique index on `account_id` covering only rows
        # where `consumed_at` is null ... keeps at most one live token per
        # account."
        #
        # Enforced by the database rather than by the service, because
        # BE-06 makes the database authoritative: two concurrent forgot
        # requests both pass a check-then-act, and only this index is
        # correct under concurrency. It is what turns "issuing a new link
        # invalidates the old one" from an intention into a guarantee —
        # which matters more here than on the verification table, since
        # the thing left alive by a lost race is a working password reset.
        #
        # Expiry is deliberately *not* in the predicate, though the doc
        # mentions it: `now()` is not immutable, so PostgreSQL rejects it
        # in an index predicate outright. The service expires rows by
        # marking them used, which this index does see.
        Index(
            "uq_password_reset_tokens__one_live_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("used_at IS NULL"),
        ),
        CheckConstraint(
            f"octet_length(token_hash) = {PASSWORD_RESET_TOKEN_HASH_LENGTH}",
            name="token_hash_length",
        ),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        {"schema": AUTH_SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{USERS_SCHEMA}.user.id", ondelete="CASCADE"),
        nullable=False,
    )

    token_hash: Mapped[bytes] = mapped_column(
        LargeBinary(PASSWORD_RESET_TOKEN_HASH_LENGTH), nullable=False
    )

    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """Set once, on redemption *or* on being superseded by a newer token.
    Both mean "cannot be redeemed again", which is exactly what this column
    says — see `OneTimeToken.consume` on why there is no fourth state."""

    id: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    def __repr__(self) -> str:
        # Never the hash — a repr lands in logs and tracebacks.
        return f"<PasswordResetTokenModel id={self.id!r} user_id={self.user_id!r}>"
