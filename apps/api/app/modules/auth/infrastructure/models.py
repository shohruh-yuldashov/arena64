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

from sqlalchemy import CheckConstraint, ForeignKey, Index, LargeBinary, String
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
