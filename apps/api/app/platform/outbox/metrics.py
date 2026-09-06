"""What the relay publishes about itself — A64-028.6 §3 and §19.

## The gap

The outbox is the platform's delivery guarantee and, until this task, it
emitted **no metrics at all**. Every failure it has ever had was found by
reading a 150 MB log file after the fact: A64-028.4's dead relay, A64-028.5A's
deadlock, and P2-9 — which is still open and is the reason this file states
reasons rather than only counts.

## Why `reason` is an enum and not an exception name

`type(error).__name__` is unbounded: a driver can invent one, and a label
that a dependency controls is a cardinality bomb pointed at the metrics
backend. The exception type stays in the log line, where it is free; the
metric carries a closed classification an alert can be written against.
"""

from enum import StrEnum
from typing import Final

#: Entries claimed by a tick. The denominator for everything below.
CLAIMED: Final = "outbox.claimed_total"

#: Entries a tick marked published.
PUBLISHED: Final = "outbox.published_total"

#: Entries a tick recorded as failed, by classified reason.
FAILED: Final = "outbox.failed_total"

#: Entries that have spent their last attempt, by reason. **The P2-9
#: metric.** An increase here is permanent loss, and before this task the
#: only way to see it was a `SELECT count(*)` somebody thought to run.
EXHAUSTED: Final = "outbox.exhausted_total"

#: Attempts that were spent without any outcome being written back — the
#: exact P2-9 signature. See `ClaimObservation`.
UNRECORDED_ATTEMPTS: Final = "outbox.unrecorded_attempts_total"

#: A tick that claimed entries and published fewer than it claimed while
#: reporting no failures. The anomaly A64-028.5A's soak logs contain 163 of
#: and nothing counted.
INCOMPLETE_TICKS: Final = "outbox.incomplete_ticks_total"

#: Wall-clock seconds a tick took, end to end.
TICK_DURATION: Final = "outbox.tick_duration_seconds"

#: Read at scrape time — see `platform/metrics/prometheus.py` on gauges.
BACKLOG: Final = "outbox.backlog"
OLDEST_PENDING_AGE: Final = "outbox.oldest_pending_age_seconds"


class FailureReason(StrEnum):
    """Why an entry did not get published, at the granularity an alert can
    branch on.

    Deliberately coarse. An operator paged at 3am needs to know whether the
    platform is failing to *reach* something, failing to *understand*
    something, or failing to *finish in time* — three different responses.
    The exact exception is one log line away.
    """

    #: A consumer raised. The common case, and the one that retries usefully.
    HANDLER_ERROR = "handler_error"
    #: A consumer exceeded its budget and was cancelled mid-delivery.
    TIMEOUT = "timeout"
    #: The payload could not be read into the shape a consumer expects — a
    #: schema mismatch, which retrying cannot fix. A64-028.5A's P1-11 was
    #: 1 850 of these and looked identical to a transient failure.
    INVALID_PAYLOAD = "invalid_payload"
    #: Anything the relay could not classify.
    UNKNOWN = "unknown"


class ExhaustionReason(StrEnum):
    """How an entry came to spend its last attempt.

    The distinction P2-9 needs. An entry that failed five times with a
    recorded reason is a delivery problem; an entry whose attempts were
    spent with **nothing written back** is a relay problem, and the two were
    indistinguishable when the only evidence was a count.
    """

    #: Five recorded failures. The intended path.
    REPEATED_FAILURE = "repeated_failure"
    #: Attempts spent with `last_error` never written — the tick claimed the
    #: row and then neither published nor failed it. See `UNRECORDED_ATTEMPTS`.
    UNRECORDED = "unrecorded"


#: Reasons a claimed entry arrives already bearing a spent attempt nobody
#: wrote an outcome for.
class ClaimObservation(StrEnum):
    """What a claim noticed about the row it just took.

    Emitted at claim time because that is the **only** moment the evidence
    exists: a row that was claimed and abandoned carries `claimed_at` set,
    `attempt_count` raised and `last_error` still null, and the next claim
    is what overwrites all three. A64-028.5A could reconstruct this only
    because the rows happened to still be in the table.
    """

    #: `attempt_count > 0`, `last_error IS NULL`, `next_attempt_at IS NULL`.
    UNRECORDED_ATTEMPT = "unrecorded_attempt"
    #: A previous attempt failed and said why. Normal retry.
    RECORDED_FAILURE = "recorded_failure"
    #: First attempt.
    FIRST_ATTEMPT = "first_attempt"


__all__ = [
    "BACKLOG",
    "CLAIMED",
    "EXHAUSTED",
    "FAILED",
    "INCOMPLETE_TICKS",
    "OLDEST_PENDING_AGE",
    "PUBLISHED",
    "TICK_DURATION",
    "UNRECORDED_ATTEMPTS",
    "ClaimObservation",
    "ExhaustionReason",
    "FailureReason",
]
