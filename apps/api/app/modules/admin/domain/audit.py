"""The audit trail's vocabulary — A64-024.8.

`database.md` §10.4 specified `admin.audit_entry` before anything needed it:
`id`, `actor_type`, `actor_id`, `action`, `subject_type`, `subject_ref`,
`before jsonb`, `after jsonb`, `correlation_id`, `created_at` — **append-only**.
`domain-model.md` §13.4 says why it is an entity rather than a log line:
"logs have a retention policy set for debugging and an access model set for
engineers. An audit trail has a retention policy set by policy or regulation
and an access model set by governance."

This module implements that schema. It does not redesign it.

## `actor_type` is what makes bootstrap honest

The first administrator on a deployment is granted by an operator command,
not by an administrator — there is none yet. A trail that recorded a
fabricated account id there would be a trail nobody can trust, and one that
recorded nothing would hide the single most privileged action the platform
has.

The documented schema already answers it: `actor_type` distinguishes an
authenticated administrator from an operator process, and `actor_id` is
nullable for the second. Nothing is invented here — the field was waiting.

## Actions are a closed vocabulary

`action` is a member of `AuditAction`, not a free string. A trail whose
action names are prose cannot be filtered, counted or alerted on, and two
producers spelling the same event differently is the failure that only
appears when somebody is trying to answer a question under pressure.

Only actions this platform **actually performs** are members. A64-024.8
introduces no mutation of its own; the two below already exist as operator
commands (A64-024.1), and they are the trail's first producers. Adding a
member is one line when a real mutation arrives — §4 forbids pre-declaring
a hundred futures.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class AuditActorType(StrEnum):
    """Who performed the action.

    Two members, and the second is not a placeholder: it is how the
    platform records work that legitimately has no signed-in actor.
    """

    ADMINISTRATOR = "administrator"
    """An authenticated account holding a role. `actor_id` is their account,
    taken from the server-side session and never from a payload."""

    OPERATOR = "operator"
    """A process run by whoever can reach the host — `app/operator/`.

    `actor_id` is **null**, and that is the truthful record rather than a
    gap: the process boundary is the authorisation (see
    `app/operator/__init__.py`), and there is no account behind it to name.
    A deployment's first `admin.role.grant` is exactly this.
    """


class AuditAction(StrEnum):
    """What happened. Closed, and small on purpose.

    Values are dotted and namespaced like every other machine-readable
    vocabulary on this platform, so a future `moderation.*` family sorts
    together and a filter can match a prefix.
    """

    ROLE_GRANTED = "admin.role.grant"
    ROLE_REVOKED = "admin.role.revoke"

    SANCTION_APPLIED = "admin.sanction.apply"
    """A64-024.6 — an account was restricted, with the case that authorised
    it named in `after`."""

    SANCTION_LIFTED = "admin.sanction.lift"
    """A64-024.6 — a restriction was ended by a named administrator, which
    §13.3 requires to be auditable in its own right."""


class AuditSubjectType(StrEnum):
    """What the action was performed on.

    Closed so the console can map a subject to a route it actually has —
    §16 forbids following a URL from the record itself. An unknown member
    cannot exist, so a link can never point somewhere unintended.
    """

    ACCOUNT = "account"


class AuditOutcome(StrEnum):
    """How it ended.

    `SUCCEEDED` is the only value A64-024.8 writes, because a success entry
    is written inside the mutation's own transaction and a rolled-back
    mutation therefore leaves none. `FAILED` exists for the failed-attempt
    seam `specs/admin.md` documents — it is deliberately unused rather than
    absent, so a later phase records a refusal without a schema change.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One privileged action, recorded.

    Frozen, and it is the point rather than a habit: an audit entry is a
    statement about a moment, and a type whose instances could be edited
    would be one a caller could rewrite before it was stored. The
    append-only guarantee starts here and is enforced again by the
    repository and by a database trigger.
    """

    id: UUID
    actor_type: AuditActorType
    actor_id: UUID | None
    action: AuditAction
    subject_type: AuditSubjectType
    subject_ref: str
    outcome: AuditOutcome
    created_at: datetime
    correlation_id: str | None = None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    """The safe, **typed** slices of state either side of the action.

    Never a request body, never a serialised entity. §8 forbids
    `metadata = request.json()`, and the way to make that structural is for
    the only writer to be a use case that names its own fields — see
    `AuditRecorder`.
    """

    def __post_init__(self) -> None:
        if self.actor_type is AuditActorType.ADMINISTRATOR and self.actor_id is None:
            # An administrator with no account is a record that cannot be
            # attributed, which is the one thing this table exists to
            # prevent. Refused at construction rather than at the database,
            # so the failure names the rule.
            raise ValueError("an administrator audit entry must name an account")
        if self.actor_type is AuditActorType.OPERATOR and self.actor_id is not None:
            raise ValueError("an operator audit entry has no account to name")


__all__ = [
    "AuditAction",
    "AuditActorType",
    "AuditEntry",
    "AuditOutcome",
    "AuditSubjectType",
]
