"""What the analytics pipeline counts about **itself** — analytics.md §54.

Operational metrics, not product analytics, and the distinction is §3's:
these say whether the pipeline is healthy, not whether anybody is playing.
They go to `app/platform/metrics`, which is the platform's operational
recorder, and they never reach the analytics event store.

Every label is a member of a closed enum, so no series can be created by
data. No actor, no subject key, no event id, no anonymous id — a
per-identity label would turn a counter into a second, worse analytics
store with no retention policy and no erasure path.

`event_name` is deliberately **not** a label either. Twenty names times
three results is sixty series for a question — "which event is failing" —
that the `WARNING` log line already answers with more detail and without
unbounded growth if the taxonomy grows.
"""

from enum import StrEnum
from typing import Final

#: Analytics events reaching the store, by what happened to each.
#:
#: One counter with a `result` label rather than three names, because the
#: questions are comparative: what share of ingestion was duplicate work,
#: and is that share rising. A rising duplicate rate means the relay is
#: retrying something and is worth seeing before it becomes a backlog.
ANALYTICS_EVENTS_INGESTED: Final = "analytics.events_ingested_total"

#: Events refused, by why. A rising `unreadable_payload` is a contract
#: problem — a domain event's shape changed under a projection — and is the
#: alert this counter exists for.
ANALYTICS_EVENTS_REJECTED: Final = "analytics.events_rejected_total"

#: Raw events deleted by the retention job. Counted so a prune that stops
#: finding anything is visible before the table is a year past its horizon.
ANALYTICS_RETENTION_DELETED: Final = "analytics.retention_deleted_total"


class IngestionResult(StrEnum):
    """What happened to one event on its way into the store."""

    STORED = "stored"

    #: Already present. The primary key rejected it, which is
    #: at-least-once delivery working rather than failing.
    DUPLICATE = "duplicate"


class RejectionReason(StrEnum):
    """Why an event did not become a row."""

    #: A tracked domain event whose payload a projection could not read,
    #: or whose version it does not support. Skipped, not retried.
    UNREADABLE_PAYLOAD = "unreadable_payload"

    #: A client submitted a name that is not client-emittable, or one that
    #: is not in the taxonomy at all.
    NOT_CLIENT_EMITTABLE = "not_client_emittable"

    #: A client event whose properties failed their schema.
    INVALID_PROPERTIES = "invalid_properties"
