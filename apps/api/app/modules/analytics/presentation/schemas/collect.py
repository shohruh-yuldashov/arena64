"""The collector's request and response — analytics.md §39, §42.

## What the request may contain, and what it may not

Five fields per event, and the absences are the design. There is no
`actor_id`, no `environment`, no `source`, no `is_synthetic`, no
`occurred_at` and no `event_id` — not rejected if sent, **unrepresentable**,
because `extra="forbid"` means a request carrying one is a `422` before any
handler runs.

That is stronger than validating them away. A field a schema does not
declare cannot be read by mistake in a later refactor, and a reviewer
looking for "can a client set the environment" finds the answer by reading
twelve lines rather than by tracing a handler.

## Bounds

Ten events per request, because the tracker batches a page's worth and a
page does not produce ten. Properties are bounded by their own schemas; this
layer bounds the envelope so a body cannot be large before anything looks
inside it.
"""

from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ClientEventRequest(BaseModel):
    """One behavioural event as a browser offers it."""

    model_config = ConfigDict(extra="forbid")

    #: Validated against `CLIENT_EMITTABLE`, not against this length.
    event_name: str = Field(max_length=64)

    #: The client's retry identity — §27. A dedup key and nothing more: the
    #: stored id is derived from this **and** the identity the server
    #: resolved, so it confers no ability to affect anybody else's events.
    idempotency_key: UUID

    #: This browser. Opaque, generated locally, and not an authentication
    #: of any kind — §30.
    anonymous_id: UUID

    #: This visit. Never the security session identifier (§31).
    session_id: UUID | None = None

    #: Checked against the event's own closed schema. `dict[str, Any]` here
    #: and nowhere else: the boundary that types it is one layer in, and
    #: duplicating each event's shape at the HTTP edge would be two places
    #: for it to drift.
    properties: dict[str, Any] = Field(default_factory=dict)


class CollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: Annotated[list[ClientEventRequest], Field(min_length=1, max_length=10)]


class CollectResponse(BaseModel):
    """What the caller is told — §42.

    `accepted` counts what was stored, so a client can see that a retry
    deduplicated rather than failed. It reveals nothing about the store: no
    row ids, no totals, no whether anybody else sent the same thing.
    """

    accepted: int
