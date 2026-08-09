"""The `admin` schema — `role_assignment` and `audit_entry`. database.md §10.4, DB-03.

The only place in this module that knows SQLAlchemy exists. Nothing above
`infrastructure/` imports this file, and what the repository returns is a
`RoleAssignment` domain value holding no ORM type (repositories.md §3).

## Why a surrogate key here, when DB-11 prefers composite ones

DB-11 asks association relations to use composite primary keys, and names
its own exception: "an association that is itself an entity with its own
lifecycle — `friendship` has a start date, an end, and a source request, so
it carries a surrogate key and uniqueness is a separate constraint."

A grant is exactly that. It has a start, an end, and a granter, and the
*same* account may hold the same role twice over time — granted, revoked,
granted again. A composite `(account_id, role)` primary key would make the
second grant impossible to record without destroying the first, which is
the history this table exists to keep.

Uniqueness is therefore a **partial** index: at most one *live* grant per
account and role. That is the invariant that matters, and expressing it in
the database rather than in the service is what makes a race between two
operators granting at once end in an integrity error rather than in two
rows that disagree (BE-06).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DDL, CheckConstraint, Index, Text, Uuid, event
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import UtcDateTime
from app.modules.admin.domain.audit import (
    AuditAction,
    AuditActorType,
    AuditOutcome,
    AuditSubjectType,
)
from app.modules.admin.domain.roles import AdminRole

ADMIN_SCHEMA = "admin"


def _enum(python_type: type, name: str) -> PgEnum:
    """A native PostgreSQL enum, spelled the way every other one on this
    platform is — see `rating.infrastructure.models._enum`.

    `values_callable` stores the member *values* rather than the Python
    member names, and this schema declares its own type rather than
    borrowing another context's.
    """
    return PgEnum(
        python_type,
        name=name,
        schema=ADMIN_SCHEMA,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


class RoleAssignmentModel(Base):
    """One grant of one administrative role."""

    __tablename__ = "role_assignment"
    __table_args__ = (
        Index(
            "uq_role_assignment__live",
            "account_id",
            "role",
            unique=True,
            postgresql_where="revoked_at IS NULL",
        ),
        # A grant cannot be revoked before it was made. Restated here as
        # well as in the domain (BE-06) so a row written by a migration or
        # by hand — which does not go through `RoleAssignment` — cannot be
        # inconsistent either.
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_role_assignment__revoked_after_granted",
        ),
        # `granted_by` must not be the grantee. An administrator granting
        # themselves authority they did not already have is the escalation
        # this whole module exists to prevent; the service refuses it and
        # this is the copy the database cannot forget.
        CheckConstraint(
            "granted_by IS NULL OR granted_by <> account_id",
            name="ck_role_assignment__not_self_granted",
        ),
        {"schema": ADMIN_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    """DM-06's opaque `player_id`. **No foreign key** — `users` is another
    schema and DB-03 forbids cross-schema references, so an account that no
    longer exists leaves a grant that resolves to nobody and confers
    nothing."""

    role: Mapped[AdminRole] = mapped_column(_enum(AdminRole, "admin_role"), nullable=False)

    granted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    """Null **only** for a deployment's first grant — see `RoleAssignment`."""

    granted_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class AuditEntryModel(Base):
    """`admin.audit_entry` — database.md §10.4, domain-model.md §13.4.

    **Append-only, and enforced by the database rather than by convention.**
    A trigger — attached below and repeated in the migration — raises on
    `UPDATE`, `DELETE` and `TRUNCATE`, so the guarantee holds against a
    repository bug, a migration, an operator with `psql`, and an
    administrator who reached the connection. A rule that only the
    application keeps is a rule the application can forget.

    No `TimestampMixin`: `created_at` is written once and there is no
    `updated_at`, because there is no update. Adding one would be a column
    that can only ever lie.

    `subject_ref` is `text` rather than `uuid` — §10.4 spells it that way,
    and it is right: the subject of a future action may be a tournament
    round `(id, number)` or a queue key, and a column typed for one shape
    would force the next producer to invent a second table.
    """

    __tablename__ = "audit_entry"
    __table_args__ = (
        # The console's only ordering, and its keyset. `created_at` alone is
        # not unique, so the `id` tiebreak is what stops a page silently
        # skipping or repeating an entry.
        Index("ix_audit_entry__created_at_id", "created_at", "id"),
        # The two filters worth an index: "what did this administrator do"
        # and "who has ever done this". Both are the questions an incident
        # starts with, and both would otherwise scan a table that only
        # grows.
        Index("ix_audit_entry__actor", "actor_id", "created_at"),
        Index("ix_audit_entry__action", "action", "created_at"),
        # "Everything that has happened to this account" — the question
        # moderation starts from, and the one a viewer opened from a user
        # page asks. Without it that filter is a scan of a table that only
        # grows.
        Index("ix_audit_entry__subject", "subject_type", "subject_ref", "created_at"),
        # `AuditEntry.__post_init__` already refuses both halves of this, and
        # that is the copy the application keeps. This is the copy the
        # database keeps for a row that arrived some other way — a
        # migration, a backfill, or a future writer that skipped the domain.
        CheckConstraint(
            "(actor_type = 'administrator' AND actor_id IS NOT NULL) "
            "OR (actor_type = 'operator' AND actor_id IS NULL)",
            name="ck_audit_entry__actor_matches_type",
        ),
        {"schema": ADMIN_SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)

    actor_type: Mapped[AuditActorType] = mapped_column(
        _enum(AuditActorType, "audit_actor_type"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    """Null exactly when `actor_type` is `operator` — see `AuditEntry`."""

    action: Mapped[AuditAction] = mapped_column(_enum(AuditAction, "audit_action"), nullable=False)
    subject_type: Mapped[AuditSubjectType] = mapped_column(
        _enum(AuditSubjectType, "audit_subject_type"), nullable=False
    )
    subject_ref: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[AuditOutcome] = mapped_column(
        _enum(AuditOutcome, "audit_outcome"), nullable=False
    )

    before: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    """Typed slices written by a use case, never a serialised request — §8."""

    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


#: The append-only guard, as DDL attached to the table itself.
#:
#: Attached here — rather than only in the migration — so that the guarantee
#: exists in **every** database this table exists in, including the ones the
#: contract suite builds with `create_all`. An invariant that holds in
#: production and not in the tests is an invariant nothing tests.
#:
#: Three statements rather than one script: asyncpg prepares every statement
#: it is given, and a prepared statement may hold only one command.
#:
#: `TRUNCATE` needs its own statement-level trigger — it fires no row trigger,
#: so a row-level guard alone leaves the single statement that empties the
#: whole trail unguarded.
_APPEND_ONLY_GUARD = (
    f"""
    CREATE OR REPLACE FUNCTION {ADMIN_SCHEMA}.audit_entry_is_append_only()
    RETURNS trigger AS $$
    BEGIN
        -- Built by concatenation rather than a format placeholder: this same
        -- DDL is executed through SQLAlchemy, where the percent sign is the
        -- driver's own parameter marker and would be consumed before
        -- PostgreSQL saw it.
        RAISE EXCEPTION USING
            ERRCODE = 'restrict_violation',
            MESSAGE = 'admin.audit_entry is append-only (attempted '
                      || TG_OP || ')';
    END;
    $$ LANGUAGE plpgsql
    """,
    f"""
    CREATE TRIGGER audit_entry_append_only
        BEFORE UPDATE OR DELETE ON {ADMIN_SCHEMA}.audit_entry
        FOR EACH ROW EXECUTE FUNCTION {ADMIN_SCHEMA}.audit_entry_is_append_only()
    """,
    f"""
    CREATE TRIGGER audit_entry_no_truncate
        BEFORE TRUNCATE ON {ADMIN_SCHEMA}.audit_entry
        FOR EACH STATEMENT EXECUTE FUNCTION {ADMIN_SCHEMA}.audit_entry_is_append_only()
    """,
)

for _statement in _APPEND_ONLY_GUARD:
    event.listen(
        AuditEntryModel.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="postgresql"),  # type: ignore[no-untyped-call]
    )


__all__ = ["ADMIN_SCHEMA", "AuditEntryModel", "RoleAssignmentModel"]
