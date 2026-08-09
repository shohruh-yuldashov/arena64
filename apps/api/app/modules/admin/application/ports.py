"""What `admin` needs from storage — repositories.md §2.

One port, three methods, and each is a question the authorization path or
the operator command actually asks. There is deliberately no `list_all`, no
`get(id)` and no update: a grant is written once and revoked once, and a
repository that could edit one would be a repository that can rewrite who
held authority when.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

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


__all__ = ["RoleAssignmentRepository"]
