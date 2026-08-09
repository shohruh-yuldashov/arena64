"""What the admin Audit API returns — A64-024.8 §11.

**Facts, not sentences.** Every field is a stored value or a name resolved
from one; nothing here is a rendered phrase. "Sanjar granted admin to Aziza"
is composed by the console in the operator's own language, from
`action`, `actor` and `subject` — a server that returned that string would
be a server that has to be redeployed to add a language.

## What deliberately cannot appear

The trail's `before`/`after` are written by use cases as typed slices, and
this schema publishes them as-is because that is what they are. What is not
here, and has no field to arrive in: credentials, tokens, OTP material,
session identifiers, raw headers, cookies, request bodies, provider
responses. Those are forbidden at the *writing* end (`AuditRecorder`), which
is the only end where forbidding them works — a response model cannot redact
what was already stored.

`correlation_id` is present because it is what joins an entry to the logs of
the same request during an incident. It identifies a request, not a person.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuditActor(BaseModel):
    """Who acted.

    `account_id` and `username` are both absent for an operator action, and
    that absence is the fact: the deployment's first grant is made from a
    shell with no administrator behind it. The console renders it as
    "operator" rather than inventing a name for nobody.

    `username` is `None` for an account that no longer exists, which is a
    real state after erasure — the trail keeps the id it recorded, and
    outlives the account it names.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(description="`administrator` or `operator`.")
    account_id: UUID | None = None
    username: str | None = None


class AuditSubject(BaseModel):
    """What was acted upon.

    `type` and `ref` together, rather than a typed id, because the subject
    of a future action need not be an account. The console maps known types
    to links and renders an unknown one as plain text — see `specs/admin.md`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    ref: str
    username: str | None = Field(
        default=None,
        description="Resolved only when `type` is `account`, batched per page. "
        "`None` for any other subject type and for an account that no longer exists.",
    )


class AuditEntryResponse(BaseModel):
    """One entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    action: str = Field(description="The semantic identifier, e.g. `admin.role.grant`.")
    outcome: str
    actor: AuditActor
    subject: AuditSubject
    before: dict[str, Any]
    after: dict[str, Any]
    correlation_id: str | None = None
    created_at: datetime


class AuditPageResponse(BaseModel):
    """One page, and the cursor that continues it.

    **No total count**, for the reason no other admin page has one — and
    more so here: the trail only ever grows, so a count gets slower every
    day the platform runs and answers a question nobody asked.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[AuditEntryResponse]
    next_cursor: str | None = None


__all__ = [
    "AuditActor",
    "AuditEntryResponse",
    "AuditPageResponse",
    "AuditSubject",
]
