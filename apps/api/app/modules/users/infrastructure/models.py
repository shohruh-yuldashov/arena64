"""The `users` ORM model — SQLAlchemy 2 typed mappings.

Owned exclusively by this module (database.md DB-03/DB-04: a module owns
its tables, nothing else writes them). Lives in its own `users` schema so
that the extraction seam of architecture.md §16 stays real, and carries no
foreign key to any other module's schema — cross-context references are
opaque `player_id` values (DM-06).

## Two documented deviations from the design docs

**1. `email` and `password_hash` live here, not in an `auth` schema.**
domain-model.md DM-10 and database.md §3.1 split this into two aggregates
in two schemas: `auth.account` (email, credentials, security state) and
`users.player_profile` (handle, display name, avatar, preferences), joined
only by an opaque `player_id`. That split exists for concrete reasons —
credential data has a different access model, a different erasure
obligation, and must be reachable without loading a profile — and it is
what lets a player exist without an account at all (a bot seat, a guest).

This task specifies a single `User` with all of those fields. Per
CLAUDE.md's precedence rule the task wins, and it is a reasonable call for
a platform this early: one table is simpler until `auth` actually exists.
But it is a deviation, not an oversight, and A64-011 inherits the cost —
see the task summary's recommendations for the concrete split path.

**2. `is_verified` is a column here** for the same reason, though the
event that sets it (proving control of an address) is `auth`'s flow.

## What is deliberately absent

`rating`, `wins`, `losses`, `draws`, `games_played`,
`leaderboard_position`, `online_status`. Each belongs to another module's
aggregate or is a projection over match history (domain-model.md §11.5:
rank is a position in an ordering over *other* players, never a column on
a player). A column here would make this module unextractable and would
put a value that changes on every completed match on the row read by every
page render.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Computed,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import Locale
from app.database.base import Base
from app.database.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import UtcDateTime
from app.modules.users.domain.privacy import (
    DEFAULT_SHOW_ACTIVITY,
    DEFAULT_SHOW_COUNTRY,
    DEFAULT_SHOW_LAST_SEEN,
    DEFAULT_SHOW_ONLINE_STATUS,
    DEFAULT_SHOW_STATISTICS,
)
from app.modules.users.domain.validators import (
    AVATAR_OBJECT_KEY_MAX_LENGTH,
    BIO_MAX_LENGTH,
    COUNTRY_CODE_LENGTH,
    DISPLAY_NAME_MAX_LENGTH,
    DISPLAY_NAME_MIN_LENGTH,
    EMAIL_MAX_LENGTH,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
)

USERS_SCHEMA = "users"


def _sql_bool(value: bool) -> str:
    """A Python bool as the SQL literal a `server_default` needs.

    Exists so the five privacy columns take their database defaults from
    `domain/privacy.py`'s constants rather than from hand-written `"true"`
    strings beside them. The two would agree on the day they were written
    and would disagree the first time a default is reconsidered — and the
    disagreement would only show up for rows the application did not
    insert, which is the hardest place to notice it (BE-06).
    """
    return "true" if value else "false"


class UserModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The `users.user` row.

    Table name is singular per database.md §2.2, which makes it the
    reserved word `user` — legal and unambiguous when schema-qualified
    (`users.user`), verified against PostgreSQL 17. SQLAlchemy quotes it
    automatically; hand-written SQL against it needs `users."user"` or the
    schema qualifier.

    Composes `UUIDPrimaryKeyMixin` (application-generated UUIDv7 — DB-07)
    and `TimestampMixin` (`created_at`/`updated_at`, with the database
    defaults acting as the backstop DB-19 describes, not the primary
    mechanism). Deliberately does **not** compose `SoftDeleteMixin`:
    DB-20 forbids a generic `deleted_at`, and `is_active` is the named
    domain state that actually applies here.
    """

    __tablename__ = "user"

    # Fetches server-generated values with `RETURNING` in the same
    # statement as the INSERT/UPDATE, rather than leaving them expired for
    # a later lazy reload.
    #
    # Without this, `username_folded` (a `Computed` column the database
    # populates) is marked expired after every flush, and the *next*
    # attribute read on that instance triggers a synchronous refresh —
    # which under asyncio raises `MissingGreenlet: greenlet_spawn has not
    # been called`, from a line that only reads `row.username`. Observed
    # directly while building A64-010's contract suite. PostgreSQL
    # supports RETURNING, so this costs no extra round trip.
    __mapper_args__ = {"eager_defaults": True}

    __table_args__ = (
        # Uniqueness is enforced on the *folded* form, not the raw one —
        # domain-model.md UP-1/AC-1 require case-insensitive uniqueness,
        # and a plain unique index on `username` would happily accept both
        # "Alice" and "alice".
        Index("uq_user__username_folded", "username_folded", unique=True),
        Index("uq_user__email", "email", unique=True),
        # Serves `list(is_active=...)`'s keyset ordering (RP-03). Ordering
        # key is `(created_at, id)`: `created_at` alone is not unique, and
        # a keyset without a unique tiebreak silently skips or repeats rows
        # at a page boundary.
        Index("ix_user__created_at_id", "created_at", "id"),
        # Interpolated from the domain's own constants, so the database's
        # authoritative bound (BE-06) cannot drift from the validator's.
        # Changing them is therefore always a migration — see
        # `3caf68aa8cfc`, which is what that costs.
        CheckConstraint(
            f"char_length(username) BETWEEN {USERNAME_MIN_LENGTH} AND {USERNAME_MAX_LENGTH}",
            name="username_length",
        ),
        CheckConstraint("char_length(email) > 0", name="email_not_empty"),
        # Interpolated from the domain's constant so the database's
        # authoritative bound (BE-06) cannot drift from the validator's,
        # exactly as `username_length` above does.
        CheckConstraint(f"char_length(bio) <= {BIO_MAX_LENGTH}", name="bio_length"),
        # Format only — membership is `reference.country`'s job once that
        # table exists. `~` is PostgreSQL's regex match.
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="country_code_format"),
        # The three avatar columns are one fact in three places, and this
        # is the database refusing to hold half of it: a key without a
        # timestamp renders an avatar nobody can date, and a timestamp
        # without a key is a row claiming an upload that is not there.
        # `User.set_avatar`/`clear_avatar` move them together; this is
        # what makes that a guarantee rather than a convention (BE-06).
        CheckConstraint(
            "(avatar_object_key IS NULL) = (avatar_uploaded_at IS NULL)",
            name="avatar_reference_is_complete",
        ),
        # A cache-buster that started below 1 would be a version a client
        # could not distinguish from "unset".
        CheckConstraint("avatar_version >= 1", name="avatar_version_positive"),
        # A64-012.3 made this field editable, so the database gains the
        # bound the domain now enforces. Interpolated from the domain's
        # own constants so the two cannot drift (BE-06), exactly as
        # `username_length` does. `NULL` passes: "no display name" is a
        # legitimate state.
        CheckConstraint(
            f"display_name IS NULL OR char_length(display_name) BETWEEN "
            f"{DISPLAY_NAME_MIN_LENGTH} AND {DISPLAY_NAME_MAX_LENGTH}",
            name="display_name_length",
        ),
        {"schema": USERS_SCHEMA},
    )

    username: Mapped[str] = mapped_column(String(USERNAME_MAX_LENGTH), nullable=False)

    username_folded: Mapped[str] = mapped_column(
        String(USERNAME_MAX_LENGTH),
        # Computed by PostgreSQL, not by the application — database.md
        # DB-21's choice, and the stronger one: a row inserted by a repair
        # script or a migration gets a correct folded value without that
        # script having to know the folding rule.
        #
        # `lower(normalize(..., NFKC))` must stay character-for-character
        # equivalent to `domain.validators.fold_username`. It currently is,
        # and `tests/contract/test_user_repository.py` asserts it against
        # real PostgreSQL rather than trusting this comment — the two
        # implementations are in different languages and would otherwise
        # drift exactly the way AD-14 describes.
        Computed("lower(normalize(username, NFKC))", persisted=True),
        nullable=False,
    )

    # Stored already-normalised (see `domain.validators.normalize_email`),
    # so this needs no generated companion — there is only ever one form.
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=False)

    # Opaque to this module: never produced, verified, or compared here.
    # Length is generous because the encoded form carries its own
    # parameters (an Argon2id encoding is ~100 chars, but the algorithm and
    # its cost factors are expected to change — database.md §14.2 records
    # per-row parameters precisely so they can be raised without a reset).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    preferred_language: Mapped[Locale] = mapped_column(
        # A native PostgreSQL enum, per database.md DB-15: closed, stable,
        # and on a table that will hold every player — 4 bytes rather than
        # a varchar. `values_callable` stores the member *values*
        # ("en"/"ru"/"uz"), not the Python member *names* ("EN"/"RU"/"UZ"),
        # which is what every other system on the platform speaks.
        PgEnum(
            Locale,
            name="locale",
            schema=USERS_SCHEMA,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        server_default=Locale.EN.value,
    )

    # IANA name, not an offset — see `domain.validators.validate_timezone`.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # Sign-in is refused until this instant. `NULL` means unlocked, and a
    # past instant means the lock has lapsed — expiry is evaluated on read,
    # never by a job that sweeps the column (see `User.locked_until` for
    # why that direction is the safe one). No index: it is only ever read
    # for a row already located by email.
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    display_name: Mapped[str | None] = mapped_column(String(DISPLAY_NAME_MAX_LENGTH), nullable=True)
    # --- avatar (A64-012.2) ------------------------------------------------
    # database.md §4.6 specifies `avatar_object_key text`. A64-010 stored a
    # full URL here; that column is dropped by `f1c4a0d2b7e5`.
    avatar_object_key: Mapped[str | None] = mapped_column(
        String(AVATAR_OBJECT_KEY_MAX_LENGTH), nullable=True
    )

    avatar_uploaded_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    avatar_version: Mapped[int] = mapped_column(
        # `server_default` as well as a Python default, because the
        # migration backfills existing rows and DB-19 makes the database
        # value the backstop rather than the primary mechanism.
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    # A64-012.1's presentational identity. Both nullable and both `None`
    # for every row today — nothing writes them until profile editing
    # exists (that task's brief excludes it).
    bio: Mapped[str | None] = mapped_column(String(BIO_MAX_LENGTH), nullable=True)

    country_code: Mapped[str | None] = mapped_column(
        # `char(2)`, not `varchar`: an ISO 3166-1 alpha-2 code is exactly
        # two characters, and a fixed-width column is the database saying
        # so rather than accepting "United Kingdom" and discovering it at
        # render time. database.md §4.6 specifies this type for the column.
        #
        # Deliberately **no foreign key** to `reference.country`, because
        # that table does not exist yet (§201 specifies it as reference
        # data). When it arrives this gains the FK; until then the CHECK
        # below is the database's half of the guarantee and
        # `validate_country_code` is the application's.
        CHAR(COUNTRY_CODE_LENGTH),
        nullable=True,
    )

    # --- privacy (A64-012.4) -----------------------------------------------
    #
    # Five columns rather than one `jsonb`, and the choice is worth stating
    # because `jsonb` is the obvious shortcut for a settings blob.
    #
    # These are not a blob. Each one is read on the platform's most-served
    # endpoint, each has a database-level default that must apply to rows
    # this application did not insert, and each will eventually be a filter
    # (`WHERE show_online_status` on a "who is online" listing). A `jsonb`
    # column gives up the default, the NOT NULL, and the plain b-tree index
    # in exchange for schema-less growth this table does not want —
    # database.md DB-15 makes the same argument for a native enum over a
    # varchar. Adding a sixth flag is one migration, which is the friction a
    # new disclosure should have.
    #
    # `NOT NULL` throughout: "no answer" is not a state a privacy control
    # may be in, because every read path would then need a fallback and the
    # fallbacks would drift. The default is the answer.
    show_country: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text(_sql_bool(DEFAULT_SHOW_COUNTRY))
    )
    show_last_seen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text(_sql_bool(DEFAULT_SHOW_LAST_SEEN))
    )
    show_statistics: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text(_sql_bool(DEFAULT_SHOW_STATISTICS))
    )
    show_online_status: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text(_sql_bool(DEFAULT_SHOW_ONLINE_STATUS))
    )
    show_activity: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text(_sql_bool(DEFAULT_SHOW_ACTIVITY))
    )

    # --- gameplay preferences (A64-012.5) -----------------------------------
    #
    # **Two documented deviations from database.md, both required by the
    # task and both worth stating rather than absorbing.**
    #
    # 1. §4.8 specifies a separate `users.player_preference` relation, 1:1
    #    with the profile, and gives a good reason: the profile row is read
    #    by *other players* on every profile render and match card, while
    #    preferences are read only by the owner — so keeping them together
    #    widens the hottest read on the platform and makes every preference
    #    toggle invalidate a row other people are reading.
    #
    # 2. §4.9 argues against `jsonb` for preference data, because a blob has
    #    no per-key constraint and cannot be probed by index.
    #
    # A64-012.5 specifies `jsonb` on the profile, and per CLAUDE.md's
    # precedence rule the task wins. The costs are real and bounded here:
    # the document is five keys and `{}` for any account that has never
    # opened the settings screen, so the widening is bytes rather than the
    # fifteen columns §4.8 was arguing about; and §4.9's index-probe
    # argument is about the *notification* dispatcher, which this column
    # deliberately does not serve — notification preferences stay out of
    # scope precisely because they are the case that needs a relation.
    #
    # What §4.9's "no per-key constraint" costs is answered in the
    # application instead: `extra="forbid"` at the HTTP boundary rejects an
    # unknown key, and `GameplayPreferences.from_document` refuses a
    # malformed value on read (database.md RK-9's "consumers validate on
    # read"). That is weaker than a CHECK and it is the trade the task
    # chose.
    #
    # The extraction path, if the profile read ever becomes hot enough to
    # care: this column and the two locale columns move together into
    # `users.player_preference`, which is exactly the grouping
    # `User.preferences` already has — so the entity would not change.
    gameplay_preferences: Mapped[dict[str, Any]] = mapped_column(
        # `JSONB`, not `JSON`: binary storage, so PostgreSQL parses on write
        # once rather than on every read, and a GIN index becomes possible
        # if a preference ever has to be queried across players.
        JSONB,
        nullable=False,
        # `{}` rather than the full default document. A row that has never
        # been touched carries no opinion, so a later change to a platform
        # default reaches everyone who has not chosen otherwise — and
        # `from_document` fills every absent key, so an empty object and a
        # complete one are the same thing to a reader. It is also what makes
        # adding a sixth preference a code change with no backfill.
        server_default=text("'{}'::jsonb"),
    )

    # Explicit re-declarations so the reader sees the full row shape here
    # rather than having to open two mixins. Types match the mixins exactly.
    id: Mapped[uuid.UUID]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        # Never includes email or password_hash — a repr lands in logs and
        # tracebacks, and services.md §8.5 keeps personal data out of both.
        return f"<UserModel id={self.id!r} username={self.username!r}>"
