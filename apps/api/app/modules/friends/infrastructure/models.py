"""The `friends` relations — `friend_request` (database.md §7.1) and
`friendship` (§7.3).

The only place in this module that knows SQLAlchemy exists. Nothing above
`infrastructure/` imports this file, and the aggregate it maps to holds no
ORM type (repositories.md §3).

## Its own schema

`friends`, per database.md §222, which assigns one schema per bounded
context. That is what makes the extraction seam in architecture.md §16 real
rather than aspirational: a `friends` service that has to be split out takes
its schema with it.

## No foreign keys to `users.user`

Deliberate, and the thing here most likely to look like an omission —
A64-013.2 asks for "foreign keys" outright, so this is a documented
deviation rather than a miss.

Cross-context references are opaque `player_id` values (DM-06), and a
foreign key from `friends` into `users` would make the two schemas
undeployable apart, which is exactly the seam §16 exists to keep open.
`statistics.player_statistics` makes the identical choice for the identical
reason, and database.md §1611 goes further: `player_id` survives erasure as
a tombstone, which a cascade would delete.

What the constraint would have bought — a request cannot name a
non-existent player — is bought instead by the endpoint, which takes the
requester from an authenticated token and the addressee from a body that
`friend_request` will simply hold if it is wrong. A row pointing at nobody
is inert: it appears in no list, because both list queries filter on the
authenticated caller's own id.

## The three constraints that *are* here

    ck_friend_request__not_self            requester <> addressee
    ck_friend_request__responded_iff_resolved
                                           responded_at set exactly when
                                           the status is not pending
    uq_friend_request__one_pending_per_pair
                                           partial unique, pending only

The third is the load-bearing one and is **partial** for the reason
database.md §7.1 gives: a plain unique on the pair would permit only one
request ever between two players, so a friendship that ended could never be
re-requested. The partial index constrains the *live* state, which is what
FR-1 actually says, and leaves the historical rows FR-5's decline cooldown
reads untouched.

BE-06: the constraint is the authoritative check. `FriendRequestValidator`
checks the same rule first to produce a good error cheaply, and two
concurrent sends both pass that check — only this index is correct under
concurrency.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, Integer, Uuid, text
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins.timestamp import TimestampMixin
from app.database.mixins.uuid_pk import UUIDPrimaryKeyMixin
from app.database.types import UtcDateTime
from app.modules.friends.domain.friend_request import FriendRequestStatus
from app.modules.friends.domain.friendship import FriendshipEndReason

#: database.md §222 — one schema per bounded context.
FRIENDS_SCHEMA = "friends"

#: `friends.friend_request_status`. A native PostgreSQL enum per DB-15:
#: closed, stable, and on a column that is filtered by every list query, so
#: four bytes beats a string and a typo cannot become a value no read path
#: knows how to evaluate.
#:
#: `values_callable` stores the member *values*, not the Python member
#: names, matching every other enum column on the platform.
_STATUS_ENUM = PgEnum(
    FriendRequestStatus,
    name="friend_request_status",
    schema=FRIENDS_SCHEMA,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class FriendRequestModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The `friends.friend_request` row.

    Composes `UUIDPrimaryKeyMixin` (application-generated UUIDv7, DB-07) and
    `TimestampMixin` (`created_at`/`updated_at`). Deliberately not
    `SoftDeleteMixin`: nothing here is ever deleted, so there is no state a
    `deleted_at` would record — DB-20 forbids a generic one anyway, and the
    named `status` is what actually applies.
    """

    __tablename__ = "friend_request"
    __table_args__ = (
        CheckConstraint("requester_id <> addressee_id", name="ck_friend_request__not_self"),
        # `responded_at` set exactly when the status is not pending —
        # database.md §7.1. Enforced here as well as in `FriendRequest._resolve`
        # so a row written by a repair script or a future sweep cannot claim
        # an outcome without its instant (BE-06).
        CheckConstraint(
            "(status = 'pending') = (responded_at IS NULL)",
            name="ck_friend_request__responded_iff_resolved",
        ),
        # FR-1, and **partial**: one *live* request per ordered pair, with
        # every historical row left alone. See this module's docstring.
        Index(
            "uq_friend_request__one_pending_per_pair",
            "requester_id",
            "addressee_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # The two list endpoints, one index each. Both are keyset queries
        # ordered by `(created_at DESC, id DESC)` filtered on one party, so
        # the index carries the filter column first and the ordering key
        # after it — which is what lets PostgreSQL walk the index backwards
        # and stop at the page size instead of sorting the whole result.
        Index("ix_friend_request__addressee", "addressee_id", "created_at", "id"),
        Index("ix_friend_request__requester", "requester_id", "created_at", "id"),
        {"schema": FRIENDS_SCHEMA},
    )

    requester_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    addressee_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    status: Mapped[FriendRequestStatus] = mapped_column(
        _STATUS_ENUM, nullable=False, server_default=FriendRequestStatus.PENDING.value
    )

    responded_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    """When this request lapses. **Always `NULL` today** — A64-013.2
    excludes expiry.

    The column exists from the first release so that adding expiry is a
    policy change and a backfill rather than an `ALTER TABLE` on a relation
    with live rows and two hot indexes. Nullable because `NULL` means *no
    window*, which is not the same as *never expires* and will stop reading
    the same the moment a sweep exists.

    Deliberately **not** indexed. An expiry sweep wants
    `WHERE status = 'pending' AND expires_at < now()`, which is a partial
    index on a column no row currently populates — an index that would be
    empty, maintained on every write, and used by nothing. It arrives with
    the sweep.
    """

    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """Optimistic-concurrency token — repositories.md §8.4 names
    `FriendRequest` status transitions as one of exactly two places on the
    platform that need one.

    The race is real and has a visible wrong outcome: an addressee with the
    request open on two devices can accept on one and decline on the other,
    and without this both writes succeed with the row's final state decided
    by arrival order. `SqlAlchemyFriendRequestRepository.resolve` matches on
    `(id, version)`, so the second write matches no row and its caller is
    told the request has already been resolved — which is true.
    """


#: `friends.friendship_end_reason`. Declared with both members from the
#: first release even though only `removed` has a producer — `ALTER TYPE ...
#: ADD VALUE` on a type used by a live table is a migration nobody should
#: have to schedule to ship blocking.
_END_REASON_ENUM = PgEnum(
    FriendshipEndReason,
    name="friendship_end_reason",
    schema=FRIENDS_SCHEMA,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class FriendshipModel(UUIDPrimaryKeyMixin, Base):
    """The `friends.friendship` row — DB-12's canonical-pair pattern.

    **One row per unordered pair**, never two mirrored ones: "two rows for
    one relationship is two facts that can disagree, and when they do,
    neither is authoritative — there is no principled repair." The read
    convenience mirroring would buy is bought instead with two indexes on
    one row (§12.3), which costs index space rather than correctness.

    Composes `UUIDPrimaryKeyMixin` (DB-07) but **not** `TimestampMixin`,
    unlike `FriendRequestModel`. A friendship has a `created_at` and an
    `ended_at`, and there is no third thing to update: the row is written
    once and ended once, so an `updated_at` would be a column that only ever
    equals one of the other two.
    """

    __tablename__ = "friendship"
    __table_args__ = (
        # DB-12: "without it, `(B, A)` is insertable and the unique
        # constraint does not fire, so the invariant fails exactly once —
        # silently, in production, under the concurrency that produced the
        # out-of-order write."
        CheckConstraint("player_low_id < player_high_id", name="ck_friendship__canonical_order"),
        # `ended_at` and `ended_reason` are set together or not at all, so a
        # row cannot record an end without saying why (BE-06). `Friendship.end`
        # writes both in one statement; this is the copy that also binds a
        # repair script.
        CheckConstraint(
            "(ended_at IS NULL) = (ended_reason IS NULL)",
            name="ck_friendship__ended_pairing",
        ),
        # One **live** friendship per pair. Partial for the reason
        # `uq_friend_request__one_pending_per_pair` is: a plain unique would
        # mean a pair whose friendship ended could never form another one,
        # which is not what FS-1 says.
        Index(
            "uq_friendship__pair",
            "player_low_id",
            "player_high_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        # §12.3's two indexes, one per side, each partial on live rows.
        # `created_at` and `id` follow the player column because every read
        # of this relation is a keyset page ordered by them — so PostgreSQL
        # can walk each leg in order and stop at the page size.
        Index(
            "ix_friendship__low",
            "player_low_id",
            "created_at",
            "id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_friendship__high",
            "player_high_id",
            "created_at",
            "id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        {"schema": FRIENDS_SCHEMA},
    )

    player_low_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    player_high_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=text("now()")
    )

    source_request_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    """The request whose acceptance created this (database.md §7.3).

    **No foreign key**, even though the referenced table is in this same
    schema and an FK would therefore cost nothing architecturally. It is
    nullable audit provenance: a retention policy that purges resolved
    requests must not be forced to choose between deleting friendships and
    keeping requests forever, which is exactly what an FK would impose.
    """

    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    ended_reason: Mapped[FriendshipEndReason | None] = mapped_column(
        _END_REASON_ENUM, nullable=True
    )
