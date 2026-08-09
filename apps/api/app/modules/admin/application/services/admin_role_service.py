"""`AdminRoleService` — granting, revoking, and answering "may they" —
A64-024.1.

Everything that decides administrative authority on this platform goes
through this class. There is no second path: the guard reads it, the
operator command writes through it, and no route can reach the repository
directly.

## The read is per request, and that is the staleness answer

`auth.TokenClaims` carries `sub`, `jti`, `type`, `iat`, `exp`, `iss` and
`aud` — **no scope, no role**. A64-024.1 deliberately did not add one, and
this is the reason: a role baked into a fifteen-minute access token is
authority that outlives its own revocation by up to fifteen minutes, and
the window is unobservable from the server.

So authority is read from the database on **every** admin request. A
revocation takes effect on the next request, not on the next token refresh.
The cost is one indexed lookup per admin request, which is a rounding error
on a surface used by a handful of people — and it is the difference between
"demoted" and "demoted soon".

## Why granting requires a granter

`grant` takes `granted_by` and refuses `None`. The one grant that legitimately
has no granter is a deployment's first, and it is made by
`app/operator/admin.py` through `bootstrap` — a separate method, so the
nullable column cannot be reached by ordinary code that simply forgot to
pass an actor. §12's invariant is that every privileged mutation is
attributable, and a default of `None` on the ordinary path would have made
the exception the easy case.

## Why self-granting is refused

An administrator who may grant themselves a role they do not hold is not an
administrator, they are anybody who can reach the endpoint. The service
refuses it, and `ck_role_assignment__not_self_granted` is the copy the
database keeps in case a future caller bypasses the service.

Note that this does not stop an administrator granting a *second* account
they control — nothing can, and that is not this check's job. What it stops
is the escalation path where holding the ability to call `grant` is itself
enough to acquire the role.

## Every grant and revocation is audited, in the same transaction

A64-024.8. `admin.role.grant` and `admin.role.revoke` are the two privileged
mutations this service performs, and each writes an `admin.audit_entry`
**inside the unit of work that writes the grant** — so the row and its
entry commit together, or neither does. An action with no entry and an entry
with no action are equally useless to somebody reconstructing what happened,
and atomicity is what makes both impossible rather than unlikely.

The actor is `granted_by` for an ordinary grant and the account whose grant
is ending for a revocation — never anything a client supplied, because this
service never sees a request. `bootstrap` records an **operator** entry with
no account, which is the honest answer for the one grant made from a shell
before any administrator exists.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from app.core.clock import Clock
from app.core.identifiers import generate_uuid7
from app.core.unit_of_work import UnitOfWork
from app.modules.admin.application.ports import RoleAssignmentRepository
from app.modules.admin.application.services.audit_recorder import AuditRecorder
from app.modules.admin.domain.audit import AuditAction, AuditSubjectType
from app.modules.admin.domain.exceptions import (
    AlreadyGranted,
    LastAdministrator,
    NotGranted,
    SelfGrant,
)
from app.modules.admin.domain.roles import AdminRole, RoleAssignment

logger = logging.getLogger(__name__)


class AdminRoleService:
    """The one authority on who administers this platform."""

    def __init__(
        self,
        *,
        assignments: RoleAssignmentRepository,
        audit: AuditRecorder,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._assignments = assignments
        self._audit = audit
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def roles_for(self, account_id: UUID) -> frozenset[AdminRole]:
        """Every role this account holds **right now** — the guard's read."""
        return await self._assignments.live_roles_for(account_id)

    async def holders_of(self, role: AdminRole) -> Sequence[UUID]:
        """Every account currently holding `role` — A64-024.3 §10.

        Published so the Users console can annotate a page with **one**
        read instead of one per row. Cheap by construction: administrators
        are a handful of accounts, so this set is smaller than any page it
        annotates.
        """
        return await self._assignments.live_holders_of(role)

    async def live_grant(self, *, account_id: UUID, role: AdminRole) -> RoleAssignment | None:
        """One account's live grant, or `None`.

        The read behind a detail page, where `granted_at` is worth showing
        and a boolean is not enough. Read-only: nothing on this path can
        create or end a grant.
        """
        return await self._assignments.live_for(account_id, role)

    async def grant(self, *, account_id: UUID, role: AdminRole, granted_by: UUID) -> RoleAssignment:
        """Grants `role` to `account_id`, on behalf of `granted_by`.

        Raises `SelfGrant` when the two accounts are the same, and
        `AlreadyGranted` when a live grant exists — the second checked here
        for a readable error and enforced by the partial unique index under
        concurrency (BE-06).
        """
        if account_id == granted_by:
            raise SelfGrant("An administrator cannot grant a role to themselves.")

        if await self._assignments.live_for(account_id, role) is not None:
            raise AlreadyGranted(f"That account already holds {role.value}.")

        return await self._record(account_id=account_id, role=role, granted_by=granted_by)

    async def bootstrap(self, *, account_id: UUID, role: AdminRole) -> RoleAssignment:
        """The **first** grant on a deployment, with no granter.

        Its own method rather than `grant(granted_by=None)`, so the one
        legitimate unattributed grant is something a caller has to ask for
        by name. See this module's docstring.

        Refuses once **any** live holder of the role exists: after that
        there is an administrator who can grant the next one, and the
        unattributed path must close behind itself. That is what stops this
        from being a permanent back door on a running deployment.
        """
        holders = await self._assignments.live_holders_of(role)
        if holders:
            raise AlreadyGranted(
                f"{role.value} already has {len(holders)} holder(s); grant through an "
                "existing administrator instead."
            )

        return await self._record(account_id=account_id, role=role, granted_by=None)

    async def revoke(
        self, *, account_id: UUID, role: AdminRole, revoked_by: UUID | None
    ) -> RoleAssignment:
        """Ends a live grant, on behalf of `revoked_by`.

        Raises `NotGranted` when there is nothing live to revoke, and
        `LastAdministrator` when this would leave the platform with no
        administrator at all — a state no route could recover from, because
        granting requires an administrator and `bootstrap` refuses while a
        holder exists.

        The check is deliberately on `ADMIN` rather than on `role`: a future
        narrower role may legitimately fall to zero holders.

        `revoked_by` is **required but nullable**, which is not the same as
        optional: a caller has to state who is ending the grant, and `None`
        is the explicit claim "an operator process, with no account behind
        it". A64-024.8 makes that distinction visible in the trail
        (`actor_type`), so a revocation made from a shell reads as one
        rather than as an anonymous gap.

        Unlike `grant`, an unattributed revocation carries no escalation
        risk — it removes authority rather than conferring it — so this
        does not need `bootstrap`'s separate-method treatment.
        """
        assignment = await self._assignments.live_for(account_id, role)
        if assignment is None:
            raise NotGranted(f"That account does not hold {role.value}.")

        if role is AdminRole.ADMIN:
            holders = await self._assignments.live_holders_of(AdminRole.ADMIN)
            if len(holders) <= 1:
                raise LastAdministrator(
                    "Refusing to revoke the last administrator — the platform would have "
                    "no way to grant another."
                )

        async with self._unit_of_work:
            revoked = await self._assignments.revoke(assignment.revoke(at=self._clock.now()))
            # Typed slices, chosen field by field (§8). The role that ended
            # and when it had begun is what a reader needs; a serialised
            # grant object would carry whatever the row gains next, forever,
            # in a table nobody may delete from.
            await self._audit_action(
                action=AuditAction.ROLE_REVOKED,
                actor_id=revoked_by,
                subject_id=account_id,
                before={"role": role.value, "granted_at": assignment.granted_at.isoformat()},
                after={
                    "role": role.value,
                    "revoked_at": revoked.revoked_at.isoformat() if revoked.revoked_at else None,
                },
            )

        # The log line stays and is not the record — see `AuditRecorder`. No
        # email, no username: the account id is the platform's own opaque
        # reference (DM-06).
        logger.info(
            "admin_role_revoked",
            extra={"account_id": str(account_id), "role": role.value},
        )
        return revoked

    async def _record(
        self, *, account_id: UUID, role: AdminRole, granted_by: UUID | None
    ) -> RoleAssignment:
        """Writes one grant. One transaction, one log line."""
        assignment = RoleAssignment(
            id=generate_uuid7(),
            account_id=account_id,
            role=role,
            granted_by=granted_by,
            granted_at=self._clock.now(),
        )

        async with self._unit_of_work:
            stored = await self._assignments.add(assignment)
            await self._audit_action(
                action=AuditAction.ROLE_GRANTED,
                actor_id=granted_by,
                subject_id=account_id,
                # `before` stays empty: the account held nothing, and an
                # empty object says that more plainly than a fabricated
                # `{"role": null}` would.
                before={},
                after={"role": role.value, "granted_at": assignment.granted_at.isoformat()},
            )

        logger.info(
            "admin_role_granted",
            extra={
                "account_id": str(account_id),
                "role": role.value,
                "granted_by": str(granted_by) if granted_by else "bootstrap",
            },
        )
        return stored

    async def _audit_action(
        self,
        *,
        action: AuditAction,
        actor_id: UUID | None,
        subject_id: UUID,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        """Records one privileged mutation, choosing the actor's *kind*.

        The branch is the whole method. An account behind the action makes
        it an administrator entry; no account makes it an operator entry
        with `actor_id` null — never a placeholder id, because a reader
        could not tell that apart from a real grant by that account.
        """
        if actor_id is None:
            await self._audit.record_operator(
                action=action,
                subject_type=AuditSubjectType.ACCOUNT,
                subject_ref=str(subject_id),
                before=before,
                after=after,
            )
            return

        await self._audit.record_administrator(
            actor_id=actor_id,
            action=action,
            subject_type=AuditSubjectType.ACCOUNT,
            subject_ref=str(subject_id),
            before=before,
            after=after,
        )


__all__ = ["AdminRoleService"]
