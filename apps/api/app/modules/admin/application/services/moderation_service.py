"""`ModerationService` — restricting an account, and undoing it. A64-024.6.

Everything that restricts access on this platform goes through this class.
There is no second path: the route calls it, and no route can reach the
repositories directly.

## One transaction, four writes

    async with unit_of_work:
        case      = await cases.add(...)        # §13.2 — the decision
        sanction  = await sanctions.add(...)    # §13.3 — the enforcement
        revoked   = await sessions.revoke_all_for(...)       # SE-3
        await audit.record_administrator(...)   # A64-024.8

All four commit together or none does. That is not tidiness: a sanction
without its case is unattributable, a sanction whose sessions survived is
not a suspension ("a suspension that lets an existing socket keep playing
is not a suspension" — SE-3), and a restriction without an audit row is a
privileged action nobody can account for. Splitting them would create four
partial states, each of which looks like success to somebody.

## What this service deliberately does not touch

`users.User.is_active`. §6's account lifecycle draws suspension and
deactivation as different transitions from `Active`, and its ownership rule
is explicit: *"`admin` may request suspension through a published port; it
never writes account rows."* Overloading the boolean would make "did they
leave or were they removed" unanswerable, and would make a player's own
reactivation silently undo a moderator's decision.

## Refusals are audited; unauthorised attempts are not

A64-024.8 shipped `AuditOutcome.FAILED` with no producers and left the
policy open. This is it, and the line is **who is asking**:

- An authenticated administrator whose action a domain safety rule refused
  — self-restriction, the last administrator, a duplicate — writes a
  `FAILED` entry. Somebody trusted tried to do something the platform
  stopped, and that is exactly the fact an incident review needs; there
  can be at most a handful of them, because each requires a live admin
  session.
- Anybody the guard rejected — no token, no role, a revoked role — writes
  **nothing** to the audit trail. Those are unauthenticated or
  unauthorised requests, they are attacker-controlled in volume, and
  letting them append rows to an append-only table nobody may delete from
  is a denial-of-service disguised as diligence. They are already recorded
  where security events belong: the application log, with the correlation
  id.
- An infrastructure failure writes nothing either, because it cannot: the
  transaction that would have carried the entry is the one that failed.
  Those surface as logs and metrics.

A `FAILED` entry is written in its **own** transaction, and correctly so —
there is no mutation for it to be atomic with, and the refusal is the whole
fact.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.core.identifiers import generate_uuid7
from app.core.unit_of_work import UnitOfWork
from app.modules.admin.application.ports import (
    ModerationCaseRepository,
    SanctionPage,
    SanctionRepository,
    SessionRevoker,
)
from app.modules.admin.application.services.audit_recorder import AuditRecorder
from app.modules.admin.domain.audit import AuditAction, AuditOutcome, AuditSubjectType
from app.modules.admin.domain.exceptions import (
    AlreadySanctioned,
    NotSanctioned,
    ProtectedAdministrator,
    SelfSanction,
)
from app.modules.admin.domain.moderation import (
    CaseStatus,
    ModerationCase,
    ModerationCategory,
    Sanction,
    SanctionKind,
)

logger = logging.getLogger(__name__)


class ModerationService:
    """The one authority on who is restricted, and why."""

    def __init__(
        self,
        *,
        cases: ModerationCaseRepository,
        sanctions: SanctionRepository,
        sessions: SessionRevoker,
        audit: AuditRecorder,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._cases = cases
        self._sanctions = sanctions
        self._sessions = sessions
        self._audit = audit
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def suspend(
        self,
        *,
        player_id: UUID,
        category: ModerationCategory,
        reasoning: str,
        expires_at: datetime | None,
        actor_id: UUID,
        administrators: Sequence[UUID],
    ) -> Sanction:
        """Withholds authentication from `player_id`, on `actor_id`'s decision.

        `administrators` is the current holder set, passed in rather than
        read here so this service depends on no other service — the caller
        already holds `AdminRoleService` and reading it twice would be two
        answers to one question.

        Raises `SelfSanction`, `ProtectedAdministrator` or
        `AlreadySanctioned`; each writes a `FAILED` audit entry first, so a
        refused attempt by a trusted account is itself on the record.
        """
        now = self._clock.now()

        if player_id == actor_id:
            await self._record_refusal(
                actor_id=actor_id,
                action=AuditAction.SANCTION_APPLIED,
                player_id=player_id,
                refusal="self_restriction",
            )
            raise SelfSanction("An administrator cannot restrict their own account.")

        # A suspended administrator cannot sign in, so suspending the last
        # one closes the console for everybody — and unlike a role
        # revocation there is no `bootstrap` to recover through. The check
        # is on the *administrator* set rather than on the target's role so
        # that restricting one of several administrators stays possible.
        holders = set(administrators)
        if player_id in holders and len(holders) <= 1:
            await self._record_refusal(
                actor_id=actor_id,
                action=AuditAction.SANCTION_APPLIED,
                player_id=player_id,
                refusal="last_administrator",
            )
            raise ProtectedAdministrator(
                "Refusing to restrict the last administrator — nobody could sign in to undo it."
            )

        existing = await self._sanctions.live_of_kind(player_id, SanctionKind.SUSPENDED)
        if existing is not None and existing.is_effective_at(now):
            await self._record_refusal(
                actor_id=actor_id,
                action=AuditAction.SANCTION_APPLIED,
                player_id=player_id,
                refusal="already_restricted",
            )
            raise AlreadySanctioned("That account is already restricted.")

        case = ModerationCase(
            id=generate_uuid7(),
            subject_player_id=player_id,
            category=category,
            status=CaseStatus.CLOSED,
            opened_by=actor_id,
            opened_at=now,
            closed_at=now,
            decision=SanctionKind.SUSPENDED.value,
            reasoning=reasoning,
        )
        sanction = Sanction(
            id=generate_uuid7(),
            player_id=player_id,
            case_id=case.id,
            kind=SanctionKind.SUSPENDED,
            starts_at=now,
            expires_at=expires_at,
            created_at=now,
        )

        async with self._unit_of_work:
            await self._cases.add(case)
            stored = await self._sanctions.add(sanction)
            # SE-3, inside the same transaction. Every refresh credential
            # this account holds stops working at the same instant the
            # restriction becomes readable — there is no window in which
            # the sanction exists and the sessions do too.
            revoked = await self._sessions.revoke_all_for(player_id, at=now)
            await self._audit.record_administrator(
                actor_id=actor_id,
                action=AuditAction.SANCTION_APPLIED,
                subject_type=AuditSubjectType.ACCOUNT,
                subject_ref=str(player_id),
                # Typed slices, field by field. No `User` object, no
                # request body, and **no `reasoning`**: the trail records
                # that a decision was taken and where the decision is
                # written down, not a second copy of the prose.
                before={"restricted": False},
                after={
                    "restricted": True,
                    "kind": SanctionKind.SUSPENDED.value,
                    "category": category.value,
                    "case_id": str(case.id),
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "sessions_revoked": revoked,
                },
            )

        logger.info(
            "account_restricted",
            extra={
                "player_id": str(player_id),
                "kind": SanctionKind.SUSPENDED.value,
                "category": category.value,
                "sessions_revoked": revoked,
            },
        )
        return stored

    async def restore(self, *, player_id: UUID, actor_id: UUID) -> Sanction:
        """Lifts the live suspension, naming who lifted it — §13.3.

        Sessions are **not** restored: they were revoked, and revocation is
        not reversible. The account signs in again and gets new ones, which
        is the correct outcome — reinstating a credential that was live
        during a restriction would hand back whatever a compromised session
        held.
        """
        now = self._clock.now()

        live = await self._sanctions.live_of_kind(player_id, SanctionKind.SUSPENDED)
        if live is None:
            await self._record_refusal(
                actor_id=actor_id,
                action=AuditAction.SANCTION_LIFTED,
                player_id=player_id,
                refusal="not_restricted",
            )
            raise NotSanctioned("That account is not restricted.")

        async with self._unit_of_work:
            lifted = await self._sanctions.lift(live.lift(at=now, by=actor_id))
            await self._audit.record_administrator(
                actor_id=actor_id,
                action=AuditAction.SANCTION_LIFTED,
                subject_type=AuditSubjectType.ACCOUNT,
                subject_ref=str(player_id),
                before={
                    "restricted": True,
                    "kind": live.kind.value,
                    "case_id": str(live.case_id),
                    "since": live.starts_at.isoformat(),
                },
                after={"restricted": False, "lifted_at": now.isoformat()},
            )

        logger.info(
            "account_restored",
            extra={"player_id": str(player_id), "kind": live.kind.value},
        )
        return lifted

    def now(self) -> datetime:
        """This service's instant.

        Published so a route can compute an expiry against the **same**
        clock the sanction is stamped with. A handler reaching for its own
        clock would be a second source of "now", and the two disagreeing by
        a request's duration is how a one-hour restriction becomes fifty-nine
        minutes in the record and sixty in the console.
        """
        return self._clock.now()

    async def effective_for(self, player_id: UUID) -> Sequence[Sanction]:
        """Every restriction in force on this account right now.

        Read-only, and read at call time rather than cached: a lift must be
        visible on the next request, not on the next cache expiry.
        """
        return await self._sanctions.effective_for(player_id, at=self._clock.now())

    async def page(self, *, effective_only: bool, limit: int, cursor: str | None) -> SanctionPage:
        """One page of restrictions for the console.

        `effective_only` is the console's default: "who is restricted now"
        is the operational question. The full listing exists because a
        lifted sanction is history rather than a deletion, and history is
        what makes the decision reviewable.
        """
        return await self._sanctions.page(
            effective_at=self._clock.now() if effective_only else None,
            limit=limit,
            cursor=cursor,
        )

    async def _record_refusal(
        self,
        *,
        actor_id: UUID,
        action: AuditAction,
        player_id: UUID,
        refusal: str,
    ) -> None:
        """Writes the `FAILED` entry for a refused administrative attempt.

        Its own transaction, and that is correct rather than a compromise:
        there is no mutation to be atomic with, and the refusal is the
        entire fact. `refusal` is a closed identifier chosen here — never a
        message, and never anything the request supplied.
        """
        async with self._unit_of_work:
            await self._audit.record_administrator(
                actor_id=actor_id,
                action=action,
                subject_type=AuditSubjectType.ACCOUNT,
                subject_ref=str(player_id),
                outcome=AuditOutcome.FAILED,
                after={"refused": refusal},
            )

        logger.warning(
            "moderation_refused",
            extra={
                "actor_id": str(actor_id),
                "player_id": str(player_id),
                "action": action.value,
                "refusal": refusal,
            },
        )


__all__ = ["ModerationService"]
