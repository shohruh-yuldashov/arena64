"""The `admin` schema — `role_assignment`. database.md §10.4, DB-03.

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

from sqlalchemy import CheckConstraint, Index, Uuid
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import UtcDateTime
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


__all__ = ["ADMIN_SCHEMA", "RoleAssignmentModel"]
