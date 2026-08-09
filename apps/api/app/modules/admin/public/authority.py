"""What another module may ask about administrative authority — A64-024.1."""

from typing import Protocol
from uuid import UUID

from app.modules.admin.domain.roles import AdminRole


class AdminAuthority(Protocol):
    """Whether an account holds a role, right now.

    **One read and no write.** A consumer can learn that somebody
    administers the platform and cannot make them administer it — the
    narrowing every published port on this platform makes, and the one that
    matters most here.

    Answered from storage on every call rather than from a token claim, so
    a revocation is effective on the next request. See `AdminRoleService`.
    """

    async def roles_for(self, account_id: UUID) -> frozenset[AdminRole]:
        """Every role the account currently holds. Empty for everybody else."""
        ...
