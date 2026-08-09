"""`AuditRecorder` — writing the trail — A64-024.8.

The one way an audit entry is created on this platform. Everything below is
a consequence of `domain-model.md` §13.4: the trail is an *entity*, not a
log line, because its retention is set by policy and its readership by
governance rather than by whoever is debugging today.

## It joins the caller's transaction; it does not own one

There is no unit of work here, deliberately. The recorder writes through a
repository that flushes into the **session the caller is already using**, so
the intended shape is:

    async with unit_of_work:
        revoked = await assignments.revoke(...)
        await audit.record_administrator(...)

and the mutation and its entry commit together or roll back together. A
recorder that commits on its own would produce the two failure modes this
design exists to prevent: an action with no entry (the rollback happened
after the audit committed) and an entry for an action that never happened.

Which of those is worse is not worth arguing about — atomicity means
neither occurs.

## Actor is a parameter of the *call site*, never of the request

`record_administrator` takes `actor_id` and the only legitimate source for
it is the authenticated identity the guard resolved — `CurrentAdmin.id`.
Nothing here reads a request, a header or a body, so there is no path by
which a client could name the actor of its own action. That is the point of
§7's invariant and it is enforced structurally: the recorder cannot see a
request.

## `before` and `after` are typed slices, never a serialised request

Both default to empty and both are written by a use case that knows what
changed — `{"role": "admin"}`, not `request.json()`. §8 lists what may never
appear: credentials, tokens, OTP material, session identifiers, raw headers,
cookies, provider responses, whole user objects. An audit trail that
captured request bodies would be the largest unredacted store of secrets on
the platform, and it is the one store nobody is allowed to delete from.

`AuditEntryModel` cannot enforce that — a `jsonb` column takes what it is
given — so the enforcement is that every caller is a named method with named
fields, and there is no `record(**anything)`.
"""

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.common.context import current_correlation_id
from app.core.clock import Clock
from app.core.identifiers import generate_uuid7
from app.modules.admin.application.ports import AuditEntryRepository
from app.modules.admin.domain.audit import (
    AuditAction,
    AuditActorType,
    AuditEntry,
    AuditOutcome,
    AuditSubjectType,
)

logger = logging.getLogger(__name__)


class AuditRecorder:
    """Appends to the audit trail. It can do nothing else."""

    def __init__(self, *, entries: AuditEntryRepository, clock: Clock) -> None:
        self._entries = entries
        self._clock = clock

    async def record_administrator(
        self,
        *,
        actor_id: UUID,
        action: AuditAction,
        subject_type: AuditSubjectType,
        subject_ref: str,
        outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        """Records something an authenticated administrator did.

        `actor_id` is the account the guard resolved. Passing anything a
        client supplied would defeat the entry entirely — see the module
        docstring.
        """
        return await self._append(
            actor_type=AuditActorType.ADMINISTRATOR,
            actor_id=actor_id,
            action=action,
            subject_type=subject_type,
            subject_ref=subject_ref,
            outcome=outcome,
            before=before,
            after=after,
        )

    async def record_operator(
        self,
        *,
        action: AuditAction,
        subject_type: AuditSubjectType,
        subject_ref: str,
        outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        """Records something an operator process did, with **no account**.

        The deployment's first grant is the case that exists today: it is
        made from a shell by somebody holding the database credentials, and
        there is no administrator behind it because that is the grant that
        creates the first one.

        `actor_id` is therefore `NULL` rather than a fabricated account.
        Naming a placeholder would be the one lie an audit trail cannot
        afford — a reader would have no way to tell it from a real grant by
        that account — and `actor_type` is the schema's own answer to the
        question, per `database.md` §10.4.

        What authorised the action is the process boundary: reaching this
        path at all requires shell access and database credentials, which
        is a stronger control than anything the trail could record about it.
        """
        return await self._append(
            actor_type=AuditActorType.OPERATOR,
            actor_id=None,
            action=action,
            subject_type=subject_type,
            subject_ref=subject_ref,
            outcome=outcome,
            before=before,
            after=after,
        )

    async def _append(
        self,
        *,
        actor_type: AuditActorType,
        actor_id: UUID | None,
        action: AuditAction,
        subject_type: AuditSubjectType,
        subject_ref: str,
        outcome: AuditOutcome,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
    ) -> AuditEntry:
        entry = AuditEntry(
            id=generate_uuid7(),
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            subject_type=subject_type,
            subject_ref=subject_ref,
            outcome=outcome,
            created_at=self._clock.now(),
            # Taken from the ambient request context rather than passed in,
            # so every entry is joinable to the logs of the same request
            # without every call site remembering to thread it through
            # (services.md §8.1). `None` outside a request — the operator
            # command — which is honest rather than invented.
            correlation_id=current_correlation_id(),
            before=dict(before or {}),
            after=dict(after or {}),
        )

        stored = await self._entries.append(entry)

        # The log line stays, and is not the record. It is what an engineer
        # greps at 3am; the entry is what governance reads later. No
        # username, no email — the account id is the platform's own opaque
        # reference (DM-06).
        logger.info(
            "admin_action_audited",
            extra={
                "action": action.value,
                "actor_type": actor_type.value,
                "actor_id": str(actor_id) if actor_id else None,
                "subject_type": subject_type.value,
                "subject_ref": subject_ref,
                "outcome": outcome.value,
            },
        )
        return stored


__all__ = ["AuditRecorder"]
