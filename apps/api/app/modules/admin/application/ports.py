"""What `admin` needs from storage — repositories.md §2.

Two ports, and every method on them is a question the authorization path,
the operator command or the audit viewer actually asks. There is
deliberately no `list_all` and no update on either: a grant is written once
and revoked once, and an audit entry is written once and never again — a
repository that could edit one would be a repository that can rewrite who
held authority when, or what they did with it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.admin.domain.audit import (
    AuditAction,
    AuditEntry,
    AuditSubjectType,
)
from app.modules.admin.domain.roles import AdminRole, RoleAssignment


class RoleAssignmentRepository(Protocol):
    """Reads and writes administrative grants."""

    async def live_roles_for(self, account_id: UUID) -> frozenset[AdminRole]:
        """Every role this account currently holds.

        **The authorization read**, and the reason it returns a set rather
        than a boolean: the guard asks "is `ADMIN` among them", and a
        second role added later needs no new method.

        Returns an empty set for an account with no grants and for one
        whose every grant is revoked — indistinguishably, because the
        answer to "may this account act" is the same and the difference is
        not the guard's business.

        Never raises for an unknown account: an id that matches nothing
        holds nothing.
        """
        ...

    async def live_for(self, account_id: UUID, role: AdminRole) -> RoleAssignment | None:
        """The live grant of one role, or `None`.

        Distinct from `live_roles_for` because revoking needs the grant
        *itself* — its id and its `granted_at` — and the set form has
        thrown those away.
        """
        ...

    async def add(self, assignment: RoleAssignment) -> RoleAssignment:
        """Records a grant.

        Raises on a second **live** grant of the same role to the same
        account: the partial unique index is what enforces it, so two
        operators granting concurrently produce an integrity error rather
        than two rows that disagree (BE-06).
        """
        ...

    async def revoke(self, assignment: RoleAssignment) -> RoleAssignment:
        """Stores an already-revoked grant. Idempotent at the domain level —
        see `RoleAssignment.revoke`."""
        ...

    async def live_holders_of(self, role: AdminRole) -> Sequence[UUID]:
        """Every account currently holding `role`.

        The one read that is not per-account, and it exists for exactly one
        caller: the operator command refusing to revoke the **last**
        administrator. A deployment with no administrator cannot grant one
        back through any route, so that refusal is the difference between a
        mistake and a lockout.
        """
        ...


@dataclass(frozen=True, slots=True)
class AuditEntryFilters:
    """What the viewer may narrow by — **index-backed filters only**.

    Each member has an index behind it (`ix_audit_entry__actor`,
    `ix_audit_entry__action`, `ix_audit_entry__subject`), so every
    combination is a seek rather than a scan of a table whose whole nature
    is to grow and never shrink.

    **No free-text search and no `before`/`after` predicate.** The JSON
    columns carry typed slices written by a use case, and offering a search
    over them would be a query language over data whose shape varies by
    action — unindexable, and the first thing to become slow once the trail
    is a year old.

    `subject_ref` may only be given **with** `subject_type`: the index is
    ordered that way, and a bare `subject_ref` would skip its leading
    column.
    """

    action: AuditAction | None = None
    actor_id: UUID | None = None
    subject_type: AuditSubjectType | None = None
    subject_ref: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEntryPage:
    """One page, and the cursor that continues it.

    No total, for the reason no other admin page has one: an operator needs
    "are there more", and counting an append-only table is a scan that gets
    slower every day the platform runs.
    """

    entries: Sequence[AuditEntry]
    next_cursor: str | None


class AuditEntryRepository(Protocol):
    """Appends and reads the audit trail. **There is no update or delete.**

    Not as an omission — as the port's entire point. `admin.audit_entry` is
    append-only in the database too (a trigger raises on `UPDATE`, `DELETE`
    and `TRUNCATE`), and this protocol is the shape of that guarantee at
    the layer above: no caller can express the mutation, so no caller can
    make it by accident.
    """

    async def append(self, entry: AuditEntry) -> AuditEntry:
        """Writes one entry into the **caller's** transaction.

        Deliberately does not commit. The recorder is used inside the same
        unit of work as the action it records, so the mutation and its
        entry commit together or not at all — see `AuditRecorder`.
        """
        ...

    async def page(
        self, *, filters: AuditEntryFilters, limit: int, cursor: str | None
    ) -> AuditEntryPage:
        """One page, newest first, keyed on `(created_at, id)`.

        `created_at` alone is not unique — two grants in the same
        millisecond are ordinary — so the `id` tiebreak is what makes the
        keyset total rather than approximately ordered.
        """
        ...


__all__ = [
    "AuditEntryFilters",
    "AuditEntryPage",
    "AuditEntryRepository",
    "RoleAssignmentRepository",
]
