"""The `AccountRestrictionGate` dependency — A64-024.6.

In `app/api/` rather than in either module's `presentation/dependencies/`,
and the placement is forced rather than stylistic.

`domain-model.md` §6 draws the dependency as `ADMIN --> "sanctions gate" -->
AUTH`: `admin` owns the restriction, `auth` enforces it. `auth` may
therefore name `admin.public.AccountRestrictionGate` in a signature — the
published surface is what a consumer is allowed to see — but it may not
*construct* the adapter, because `.importlinter`'s
`admin-internals-are-private` contract forbids `app.modules.auth` from
importing `app.modules.admin.infrastructure`, and rightly: a module that can
build another's repository can reach anything in it.

So the wiring happens one level up, where composing two modules is the
whole job. `app/api/outbox_deps.py` established the pattern and its
docstring gives the same reason for not folding it into `app/api/deps.py`:
that module holds request-scoped *infrastructure*, and this is a service
built over one of them.

**Read-only, by type.** What is published here is the gate. Nothing in this
file can restrict an account; that is `ModerationService`, which is not
published and is reachable only through the admin console's own routes.
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import DbSessionDep
from app.modules.admin.infrastructure.repositories import SqlAlchemySanctionRepository
from app.modules.admin.public import AccountRestrictionGate


def get_account_restriction_gate(session: DbSessionDep) -> AccountRestrictionGate:
    """The per-request restriction read, over the request's session.

    The same adapter the moderation console writes through, narrowed by the
    annotation to its one published read — so a caller holding this cannot
    apply, lift or even list a restriction.
    """
    return SqlAlchemySanctionRepository(session)


AccountRestrictionGateDep = Annotated[AccountRestrictionGate, Depends(get_account_restriction_gate)]

__all__ = ["AccountRestrictionGateDep", "get_account_restriction_gate"]
