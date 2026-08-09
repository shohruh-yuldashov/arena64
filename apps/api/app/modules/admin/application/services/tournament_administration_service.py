"""`TournamentAdministrationService` — driving a tournament. A64-024.5H.

Four commands, each one a transition `tournament`'s own aggregate already
defines, each written to the audit trail in the same transaction.

This service **decides nothing about tournaments**. It does not know the
transition table, it does not seed, it does not pair, and it cannot set a
status. It calls a lifecycle command and records that an administrator
called it — which is the whole of what an administration surface should be
over a domain that already owns its rules.

## What it deliberately cannot do

There is no `publish_round` and no `cancel`, and neither is an oversight:

- **Round publication is match-driven.** `TournamentAdvancementService`
  publishes the next round when the current one completes, from real
  results. There is no manual use case to call, and a method here would
  have to invent one — an administrator injecting bracket progression the
  domain derives.
- **Cancellation is unfinished.** The aggregate permits the transition and
  `TournamentCancelled` exists as an event type, but nothing publishes it
  and nothing consumes it: what happens to matches in flight, to
  registrations, to standings and to the people who were told the
  tournament was starting has no answer in this repository. Exposing it
  would mean writing those answers here, in the wrong module, as a side
  effect of an admin button. `specs/admin.md` §6.15 records it as a product
  decision rather than a gap to be filled quietly.

## Atomicity across a module boundary

The lifecycle services commit by contract — that is correct for the
operator command line, which has nothing to add to their transaction. This
service does: the audit entry.

`ParticipatingUnitOfWork` (A64-022.3 §10) is the repository's answer and is
handed to those services at the composition root. They stage, this service
commits, and the transition and its audit entry land together or not at
all. Nothing about the tournament services changes, and their other callers
do not know this one exists.

## Refusals

A transition the aggregate refuses raises out of the lifecycle service.
Each refusal by an authenticated administrator writes a `FAILED` audit
entry — A64-024.6's policy, applied unchanged rather than re-decided, and
in its own transaction because there is no mutation for it to be atomic
with.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from app.core.unit_of_work import UnitOfWork
from app.modules.admin.application.ports import (
    TournamentLifecycle,
    TournamentLifecycleResult,
)
from app.modules.admin.application.services.audit_recorder import AuditRecorder
from app.modules.admin.domain.audit import AuditAction, AuditOutcome, AuditSubjectType
from app.modules.game.public.variants import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.domain.tournament import TournamentStatus

logger = logging.getLogger(__name__)

#: The shape of a one-argument lifecycle command, for `_transition`.
#:
#: A local alias rather than a published type: it exists so the three
#: callers read as "run this command and record it" instead of repeating the
#: body, and nothing outside this module composes one.
_Command = Callable[[UUID], Awaitable[TournamentLifecycleResult]]

#: Which audit action each command writes.
#:
#: A mapping rather than an argument, so a caller cannot record a transition
#: under another one's name — and so adding a command without deciding its
#: action fails at the lookup rather than defaulting to something plausible.
_ACTION_OF: dict[TournamentStatus, AuditAction] = {
    TournamentStatus.REGISTRATION_OPEN: AuditAction.TOURNAMENT_REGISTRATION_OPENED,
    TournamentStatus.REGISTRATION_CLOSED: AuditAction.TOURNAMENT_REGISTRATION_CLOSED,
    TournamentStatus.IN_PROGRESS: AuditAction.TOURNAMENT_STARTED,
}


class TournamentAdministrationService:
    """The audited face of `tournament`'s lifecycle commands."""

    def __init__(
        self,
        *,
        lifecycle: TournamentLifecycle,
        audit: AuditRecorder,
        unit_of_work: UnitOfWork,
    ) -> None:
        self._lifecycle = lifecycle
        self._audit = audit
        self._unit_of_work = unit_of_work

    async def create(
        self,
        *,
        name: str,
        variant: ProductVariant,
        speed_class: SpeedClass,
        capacity: int,
        rated: bool,
        registration_deadline: datetime | None,
        actor_id: UUID,
    ) -> TournamentLifecycleResult:
        """Creates a tournament in `DRAFT`, attributed to `actor_id`.

        The new tournament's id is the **server's**, and so is its state:
        nothing a caller sends decides either. `created_by` is the
        administrator, which is the one place a client-supplied value would
        have been both plausible and wrong.
        """
        async with self._unit_of_work:
            created = await self._lifecycle.create(
                name=name,
                variant=variant,
                speed_class=speed_class,
                capacity=capacity,
                rated=rated,
                registration_deadline=registration_deadline,
                created_by=actor_id,
            )
            await self._audit.record_administrator(
                actor_id=actor_id,
                action=AuditAction.TOURNAMENT_CREATED,
                subject_type=AuditSubjectType.TOURNAMENT,
                subject_ref=str(created.tournament_id),
                # The configuration, as chosen facts. **Not the tournament
                # object** — an aggregate serialised into an append-only
                # table is a copy that grows whenever the aggregate does and
                # can never be corrected.
                before={},
                after={
                    "status": created.status.value,
                    "capacity": capacity,
                    "variant": variant.value,
                    "speed_class": speed_class.value,
                    "rated": rated,
                },
            )
            await self._unit_of_work.commit()

        logger.info(
            "tournament_created_by_admin",
            extra={"tournament_id": str(created.tournament_id), "capacity": capacity},
        )
        return created

    async def open_registration(
        self, *, tournament_id: UUID, actor_id: UUID
    ) -> TournamentLifecycleResult:
        """`DRAFT` → `REGISTRATION_OPEN`."""
        return await self._transition(
            tournament_id=tournament_id,
            actor_id=actor_id,
            command=self._lifecycle.open_registration,
            from_status=TournamentStatus.DRAFT,
        )

    async def close_registration(
        self, *, tournament_id: UUID, actor_id: UUID
    ) -> TournamentLifecycleResult:
        """`REGISTRATION_OPEN` → `REGISTRATION_CLOSED`."""
        return await self._transition(
            tournament_id=tournament_id,
            actor_id=actor_id,
            command=self._lifecycle.close_registration,
            from_status=TournamentStatus.REGISTRATION_OPEN,
        )

    async def start(self, *, tournament_id: UUID, actor_id: UUID) -> TournamentLifecycleResult:
        """`REGISTRATION_CLOSED` → `IN_PROGRESS`, seeding and launching.

        The heaviest of the four: it materialises the bracket and creates
        the first round's matches. `matches_launched` reaches the audit
        entry because "moved to in_progress and launched nothing" is the
        failure an operator has to be able to see afterwards.
        """
        return await self._transition(
            tournament_id=tournament_id,
            actor_id=actor_id,
            command=self._lifecycle.start,
            from_status=TournamentStatus.REGISTRATION_CLOSED,
        )

    async def _transition(
        self,
        *,
        tournament_id: UUID,
        actor_id: UUID,
        command: _Command,
        from_status: TournamentStatus,
    ) -> TournamentLifecycleResult:
        """Runs one lifecycle command and records it.

        `from_status` is written to the entry as the state the command was
        *for*, not as a state this service read and checked. Checking it
        here would be a second copy of the transition table, and the copy
        that goes stale — the aggregate refuses under a row lock, which is
        the only place the answer cannot race.
        """
        try:
            async with self._unit_of_work:
                moved = await command(tournament_id)
                await self._audit.record_administrator(
                    actor_id=actor_id,
                    action=_ACTION_OF[moved.status],
                    subject_type=AuditSubjectType.TOURNAMENT,
                    subject_ref=str(tournament_id),
                    before={"status": from_status.value},
                    after=_after(moved),
                )
                await self._unit_of_work.commit()
        except Exception:
            # A64-024.6's policy: an authenticated administrator refused by
            # a domain rule is on the record. Written in its own
            # transaction — the one above rolled back, and there is no
            # mutation for this to be atomic with.
            await self._record_refusal(
                tournament_id=tournament_id, actor_id=actor_id, from_status=from_status
            )
            raise

        logger.info(
            "tournament_transitioned_by_admin",
            extra={"tournament_id": str(tournament_id), "status": moved.status.value},
        )
        return moved

    async def _record_refusal(
        self, *, tournament_id: UUID, actor_id: UUID, from_status: TournamentStatus
    ) -> None:
        """The `FAILED` entry for a refused transition.

        `refused` is the command that was attempted, as the state it would
        have reached — a closed identifier chosen here, never a message and
        never anything the request supplied.
        """
        async with self._unit_of_work:
            await self._audit.record_administrator(
                actor_id=actor_id,
                action=AuditAction.TOURNAMENT_TRANSITION_REFUSED,
                subject_type=AuditSubjectType.TOURNAMENT,
                subject_ref=str(tournament_id),
                outcome=AuditOutcome.FAILED,
                after={"expected_from": from_status.value},
            )
            await self._unit_of_work.commit()

        logger.warning(
            "tournament_transition_refused",
            extra={
                "tournament_id": str(tournament_id),
                "actor_id": str(actor_id),
                "expected_from": from_status.value,
            },
        )


def _after(moved: TournamentLifecycleResult) -> dict[str, object]:
    """The resulting state, and the launch count where there is one.

    `matches_launched` is omitted rather than written as zero for the three
    commands that launch nothing — a field that is always zero is one a
    reader learns to ignore on the one entry where it matters.
    """
    after: dict[str, object] = {"status": moved.status.value}
    if moved.matches_launched:
        after["matches_launched"] = moved.matches_launched
    return after


__all__ = ["TournamentAdministrationService"]
