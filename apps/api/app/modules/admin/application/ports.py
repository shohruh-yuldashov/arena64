"""What `admin` needs from storage and from its collaborators —
repositories.md §2.

Every method on every port here is a question the authorization path, the
operator command, the audit viewer or the moderation console actually
asks. There is
deliberately no `list_all` and no update on either: a grant is written once
and revoked once, and an audit entry is written once and never again — a
repository that could edit one would be a repository that can rewrite who
held authority when, or what they did with it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.admin.domain.audit import (
    AuditAction,
    AuditEntry,
    AuditSubjectType,
)
from app.modules.admin.domain.moderation import (
    ModerationCase,
    Sanction,
    SanctionKind,
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


@dataclass(frozen=True, slots=True)
class SanctionPage:
    """One page of restrictions, and the cursor that continues it."""

    sanctions: Sequence[Sanction]
    next_cursor: str | None


class ModerationCaseRepository(Protocol):
    """Writes and reads decision records. **There is no update.**

    §13.2: a case is immutable once closed and a reversal is a new case
    that references the original — "an editable moderation record cannot be
    trusted in an appeal, which is the only situation in which anybody
    reads it". The absence of an update method is where that is kept.
    """

    async def add(self, case: ModerationCase) -> ModerationCase:
        """Records one decision **in the caller's transaction**.

        Flushes, never commits: the case, the sanction it authorises, the
        session revocation and the audit entry are one unit of work.
        """
        ...

    async def cases_by_ids(self, case_ids: Sequence[UUID]) -> Mapping[UUID, ModerationCase]:
        """Every named case, in one query — the batch the console needs so
        a page of restrictions does not read a case per row.

        Incomplete on purpose: an id that matches nothing is absent rather
        than raising.
        """
        ...


class SanctionRepository(Protocol):
    """Writes and reads enforced restrictions."""

    async def add(self, sanction: Sanction) -> Sanction:
        """Records one restriction in the caller's transaction.

        Raises on a second **unlifted** sanction of the same kind for one
        account: `uq_sanction__live_kind` is what enforces it, so two
        administrators acting at once produce an integrity error rather
        than two live restrictions that disagree (BE-06).
        """
        ...

    async def lift(self, sanction: Sanction) -> Sanction:
        """Stores an already-lifted restriction. The only update this port
        offers, and it writes `lifted_at`/`lifted_by` and nothing else —
        the decision it enforced cannot be rewritten through here."""
        ...

    async def effective_for(self, player_id: UUID, *, at: datetime) -> Sequence[Sanction]:
        """Every restriction in force on this account at `at` — Q6.

        The hot authorization read (DM-12). Backed by
        `ix_sanction__player_expiry`: the index carries the immutable half
        of the predicate (`lifted_at IS NULL`) and the instant comparison
        is a filter on the handful of rows it returns, because a partial
        index predicate cannot contain `now()` (database.md §12.6).
        """
        ...

    async def live_of_kind(self, player_id: UUID, kind: SanctionKind) -> Sanction | None:
        """The unlifted sanction of one kind, or `None`.

        Distinct from `effective_for` because lifting needs the sanction
        *itself* — its id and its case — and because an **expired but
        unlifted** row is still the row a repeat restriction would collide
        with under `uq_sanction__live_kind`.
        """
        ...

    async def page(
        self, *, effective_at: datetime | None, limit: int, cursor: str | None
    ) -> SanctionPage:
        """One page, newest first, keyed on `(created_at, id)`.

        `effective_at` narrows to restrictions in force at that instant;
        `None` lists every restriction ever recorded, because history is
        what makes a lifted sanction auditable rather than deleted.
        """
        ...


class SessionRevoker(Protocol):
    """Ending every live session for an account — SE-3.

    Declared here rather than imported from `auth` because it states what
    **`admin` needs**, which is one method: *"a suspension that lets an
    existing socket keep playing is not a suspension"*. The adapter is
    `auth`'s own session repository, wired at the composition root, so the
    revocation happens inside the moderation transaction rather than in a
    second one that could commit alone.
    """

    async def revoke_all_for(self, user_id: UUID, *, at: datetime) -> int:
        """Revokes every unrevoked session; returns how many. Idempotent.

        **No `reason` parameter.** `auth` owns the vocabulary of why a
        session ended (`RevocationReason.SUSPENSION` exists for exactly
        this), and an `admin` port that named it would make this module
        import another's domain enum to describe a value it only passes
        through. The adapter binds the reason at the composition root.
        """
        ...


__all__ = [
    "AuditEntryFilters",
    "AuditEntryPage",
    "AuditEntryRepository",
    "ModerationCaseRepository",
    "RoleAssignmentRepository",
    "SanctionPage",
    "SanctionRepository",
    "SessionRevoker",
]
