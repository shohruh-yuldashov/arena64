"""The analytics event — the envelope A64-027.1 §5 froze, as a record.

Framework-free: nothing here imports SQLAlchemy, FastAPI or Pydantic, so an
event can be constructed in a unit test with no database and serialised into
a `jsonb` column with no mapper. The same rule `DomainEvent` follows, for the
same reason.

## `event_id` for a projection that fans out

§5 says `event_id` **is** `outbox.id` for a backend event, and that is what
makes redelivery a no-op: the store's primary key is the id the authoritative
source generated.

Two events in the taxonomy project **one outbox row into two analytics
rows** — `match_found` and `match_started` are per seat. One id cannot be
the primary key of two rows, so those ids are derived: `uuid5` over the
outbox id and the seat. Deterministic, so a redelivery produces the same two
ids and conflicts on both; distinct, so the primary key still holds.

`source_event_id` keeps the outbox id itself, which is what an operator
follows from an incident back to the row that caused it. This is the one
place A64-027.2 extends §5's envelope, and it is recorded in the document
rather than left as a surprise in a schema.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from app.config.environment import Environment
from app.modules.analytics.domain.subject import SubjectKey
from app.platform.analytics import EventName, Identity, spec_for

#: The namespace for derived per-seat event ids. A fixed UUID rather than a
#: generated one: the derivation must produce the same id on a redelivery in
#: a different process, next year, after a redeploy.
SEAT_NAMESPACE = UUID("6f1f0a2e-1c4b-5f8a-9d3e-0b7c2a5e4d16")


def seat_event_id(source_event_id: UUID, seat: str) -> UUID:
    """The id for one seat's row of a fan-out projection.

    `uuid5`, not `uuid4`: the whole point is that the second delivery of one
    outbox row derives the identical pair of ids and is rejected by the
    primary key.
    """
    return uuid5(SEAT_NAMESPACE, f"{source_event_id}:{seat}")


@dataclass(frozen=True, slots=True)
class AnalyticsEvent:
    """One row of the event store, before it is a row.

    Every field the server controls is required. The three optional identity
    fields are the ones §5 makes optional, and the invariant below is the
    one that makes a row queryable at all.
    """

    event_id: UUID
    event_name: EventName
    event_version: int

    #: When the fact became true. For a projection this is the domain
    #: event's own instant, staged inside the transaction that made it true.
    #: For a client event it is the **server's** receive time (§43) — a
    #: browser clock never decides a metric's bucket.
    occurred_at: datetime

    #: When the platform stored it. Distinct from `occurred_at` because the
    #: gap between them is the relay's lag, and an operator diagnosing a
    #: backlog reads exactly that difference.
    received_at: datetime

    #: `backend` or `frontend`, from the registry's `Owner`. Server-assigned
    #: in both paths — a client that could set this could claim authority.
    source: str

    environment: Environment

    #: The person, if there is one. Never a `PlayerId` — see `subject`.
    subject_key: SubjectKey | None = None

    #: One browser, for the pre-registration half of the acquisition funnel.
    anonymous_id: UUID | None = None

    #: One visit. Never the security session identifier (§31): this is a
    #: non-secret grouping key and nothing authenticates with it.
    session_id: UUID | None = None

    #: Excluded from every product metric. Server-derived from the account,
    #: never from the request (§46).
    is_synthetic: bool = False

    #: The event's own dimensions, already validated against its schema.
    #: A plain mapping here because validation has happened by the time one
    #: of these exists — the typed schema is the boundary, and carrying the
    #: model further would make every consumer import it.
    properties: dict[str, Any] = field(default_factory=dict)

    #: The outbox row a projection came from. `None` for a client event,
    #: which has no outbox row.
    source_event_id: UUID | None = None

    def __post_init__(self) -> None:
        """The identity the taxonomy declared, checked against the row.

        Three classes and three different failures, which is why this is
        not one condition:

            ACTOR      a person's event with no subject is a row no
                       per-person metric can see — a cohort silently
                       smaller than it should be
            ANONYMOUS  a browser event with no browser is the acquisition
                       funnel's first step, lost
            ENTITY     a match-level event **with** an identity is worse:
                       it looks attributable, so somebody attributes it,
                       and one game is counted for one of its two seats

        `ENTITY` is the case that argues for checking at all. The other two
        produce a number that is too small; this one produces a number that
        is wrong and looks fine.
        """
        if self.event_version < 1:
            raise ValueError(f"{self.event_name} has a non-positive version")

        identity = spec_for(self.event_name).identity
        if identity is Identity.ENTITY:
            if self.subject_key is not None or self.anonymous_id is not None:
                raise ValueError(f"{self.event_name} is entity-level and carries an identity")
            return

        if identity is Identity.ACTOR and self.subject_key is None:
            raise ValueError(f"{self.event_name} needs a subject")

        if identity is Identity.ANONYMOUS and self.anonymous_id is None:
            raise ValueError(f"{self.event_name} needs an anonymous id")
