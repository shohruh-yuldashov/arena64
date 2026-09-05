"""The administrative broadcast — A64-027A §12–§21.

Framework-free (architecture.md §8). A `Broadcast` is a *request to tell
people something*, and it is a persisted aggregate rather than a function
call for one reason: §19 forbids looping over thousands of recipients inside
an HTTP request, so the admin request must produce a durable unit of work
that a worker finishes afterwards.

## What this is not

It is not a message. `public/administration.py` states the rule this file
had to clear: until now there was "no way through this port to create a
notification, choose a recipient, choose a type, choose a payload or choose
a destination", and that absence was what made the admin port safe. A
broadcast reopens exactly one of those — the recipient set and the text —
and closes the others by construction:

    type          always `PLATFORM_ANNOUNCEMENT`; the admin does not choose
    destination   always `HOME`; the admin cannot write a URL
    channel       always in-app; see `BroadcastChannel`
    category      always `ANNOUNCEMENT`, which is **mutable**, so a player
                  who muted it receives nothing

## Idempotency is the recipient's row, not a lock

Two things can duplicate a broadcast, and they need different answers.

A double-submitted *form* is answered by `idempotency_key`: the client mints
one per composition, the table holds a unique index on it, and a second POST
returns the first broadcast instead of creating a second.

A re-run *batch* — a worker that crashed after writing rows and before
recording progress — is answered by `notification_id_for`, which derives the
recipient's `source_event_id` from the broadcast and the player. The
notification table's `(recipient_id, source_event_id, type)` unique
constraint then turns the re-run into `ON CONFLICT DO NOTHING`. No lease, no
distributed lock, and a crash at any point costs at most a repeated batch
that writes nothing.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid5

#: Fixed, not generated: a redelivery next year must derive the same id as
#: the delivery that crashed today, in a different process.
BROADCAST_NAMESPACE: Final = UUID("3d5c8b1a-9f42-5e07-8c6d-2b41f7a09e53")

#: Bounds on the admin-authored text. Short enough that a notification list
#: stays a list, long enough to say something. Enforced at the boundary that
#: accepts the request and again here, because the API is not the only
#: possible caller of the service.
MAX_TITLE_LENGTH: Final = 120
MAX_BODY_LENGTH: Final = 600

#: How many named recipients one broadcast may carry. A bound rather than
#: none, because the list travels in the request body and is stored inline:
#: an unbounded array is an unbounded row.
MAX_NAMED_RECIPIENTS: Final = 100


class BroadcastAudience(StrEnum):
    """Who a broadcast is for.

    Two members, and the set is short on purpose. §14 forbids inventing
    segmentation: a "lapsed players" or "high rated" audience would need a
    definition somebody could defend, and this platform has not agreed one.
    When it does, it arrives here as a member with a query behind it.
    """

    ALL_PLAYERS = "all_players"
    """Every account eligible to receive an in-app notification.

    *Eligible* is the server's definition, not the console's — active and
    verified — and the count is computed by the same query that will select
    the recipients. §14: a recipient count the frontend estimated would be a
    number an administrator trusts and nothing produced.
    """

    SPECIFIC_PLAYERS = "specific_players"
    """A named list, bounded by `MAX_NAMED_RECIPIENTS`."""


class BroadcastChannel(StrEnum):
    """How it is delivered.

    One member. Email and push both exist on this platform and neither is
    offered here, which is a deliberate deferral rather than an oversight:
    a broadcast over email is a different risk class — provider cost, sender
    reputation, a bounce path, and an unsubscribe obligation that in-app
    delivery discharges through the preference switch. §15 says to show only
    the channels that exist; showing an email toggle this build would honour
    badly is worse than showing none.
    """

    IN_APP = "in_app"


class BroadcastStatus(StrEnum):
    """Where the work has got to.

    Four members. §20 lists more — `draft`, `partially_failed` — and neither
    is here because neither has semantics this implementation can honour: a
    draft is a composer the console has not been asked for, and in-app
    delivery has no partial failure. A recipient either gets a row or is
    suppressed by their own preference, and suppression is not a failure.
    """

    QUEUED = "queued"
    SENDING = "sending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Broadcast:
    """One administrative announcement, and its progress."""

    id: UUID
    title: str
    body: str
    locale: str
    audience: BroadcastAudience
    channel: BroadcastChannel
    status: BroadcastStatus

    created_by: UUID
    """The administrator who sent it. §23 — an authority nobody can
    attribute is one nobody can review."""

    created_at: datetime
    idempotency_key: str

    #: Named recipients, for `SPECIFIC_PLAYERS`. Empty otherwise.
    recipients: tuple[UUID, ...] = ()

    #: How many accounts the audience resolved to, once a worker has
    #: counted them. `None` until then — **never** a zero standing in for
    #: "not counted yet", which would read as a broadcast that reached
    #: nobody.
    audience_size: int | None = None

    #: Rows actually written. Lower than `audience_size` by exactly the
    #: number of players who have muted the category.
    delivered: int = 0

    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Why it failed, for an operator reading the history. Never a stack
    #: trace and never a recipient — see `specs/admin.md` on what may cross
    #: into a console.
    failure_reason: str | None = None

    #: The keyset the expander resumes from: the last player id written.
    #: A cursor rather than an offset, because accounts are created while a
    #: broadcast is being delivered and an offset would skip or repeat them.
    cursor: UUID | None = field(default=None)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("a broadcast needs a title")
        if not self.body.strip():
            raise ValueError("a broadcast needs a body")
        if len(self.title) > MAX_TITLE_LENGTH:
            raise ValueError(f"title exceeds {MAX_TITLE_LENGTH} characters")
        if len(self.body) > MAX_BODY_LENGTH:
            raise ValueError(f"body exceeds {MAX_BODY_LENGTH} characters")
        if self.audience is BroadcastAudience.SPECIFIC_PLAYERS and not self.recipients:
            raise ValueError("a named audience needs at least one recipient")
        if len(self.recipients) > MAX_NAMED_RECIPIENTS:
            raise ValueError(f"more than {MAX_NAMED_RECIPIENTS} named recipients")
        if self.audience is BroadcastAudience.ALL_PLAYERS and self.recipients:
            raise ValueError("a platform-wide broadcast carries no recipient list")

    @property
    def is_finished(self) -> bool:
        return self.status in (BroadcastStatus.COMPLETED, BroadcastStatus.FAILED)


def notification_id_for(broadcast_id: UUID, player_id: UUID) -> UUID:
    """The `source_event_id` this broadcast writes for this recipient.

    Derived rather than random, which is what makes a repeated batch a
    no-op: the notification table is unique on
    `(recipient_id, source_event_id, type)`, so the second write of a row
    conflicts and is discarded. A worker may therefore crash anywhere in a
    batch and be restarted without sending anybody the same announcement
    twice.
    """
    return uuid5(BROADCAST_NAMESPACE, f"{broadcast_id}:{player_id}")
