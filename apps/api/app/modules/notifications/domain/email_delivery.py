"""One attempt to email a notification, and how it ended — A64-021.5 §6, §11, §19.

Framework-free. The state machine a durable delivery row moves through, the
bounded vocabulary of *why* it stopped, and the backoff that decides when a
retry is due.

## Why the outcomes are a closed set rather than exceptions

§6: *"Do not treat expected skips as exceptions."* Most of what happens to a
notification email is not a failure — the player muted the category, the
address is not verified, the type is not one this platform emails. Those are
answers, and raising for them would make the ordinary path an exception
handler and the operational question *"why did nobody get email"*
unanswerable without reading tracebacks.

So a delivery attempt **returns** an outcome, and only an unexpected
provider fault raises.

## Why a permanent failure keeps the row

The durable notification is untouched either way — email is secondary to it
(§2), and a player whose address bounced still has the in-app record. What
the row preserves is the *operational* answer: an operator asking "did we
try, and why did it stop" gets one, where a deleted row would look
identical to a delivery nobody ever attempted.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final


class EmailDeliveryStatus(StrEnum):
    """Where a delivery row currently stands.

    Three, and the narrowness matters: `PENDING` covers both "never tried"
    and "tried and will try again", because the two are the same question to
    the worker — *is this due* — and splitting them would give the claim
    query a second predicate that could disagree with `next_attempt_at`.
    """

    PENDING = "pending"
    """Owed. `next_attempt_at` says when."""

    SENT = "sent"
    """A provider accepted it. **Not** "somebody received it" — delivery is
    asynchronous and out of this process, and a status claiming otherwise
    would be a promise the platform cannot keep (`EmailProvider.send`)."""

    SKIPPED = "skipped"
    """Deliberately not sent. `outcome` says why, and every reason is one
    the platform decided rather than one a provider reported."""

    FAILED = "failed"
    """Abandoned. Either a permanent provider rejection or the attempt limit
    reached; `outcome` distinguishes them."""


class EmailDeliveryOutcome(StrEnum):
    """Why a delivery ended where it did — the bounded vocabulary §6 asks for.

    Every member is a **label this platform chose**, never a provider string.
    That is what keeps it safe to put on a metric: a vendor error code as a
    label would be an unbounded cardinality dimension fed by a third party.
    """

    DELIVERED = "delivered"
    """Handed to a provider, which accepted it."""

    SKIPPED_PREFERENCE = "skipped_preference"
    """The recipient muted this category on email."""

    SKIPPED_UNSUPPORTED_TYPE = "skipped_unsupported_type"
    """The notification type is not one this platform emails — see
    `domain.email.EMAIL_CAPABLE_TYPES`. Reachable when a type is *removed*
    from that set while deliveries for it are already queued."""

    SKIPPED_UNVERIFIED_EMAIL = "skipped_unverified_email"
    """The address exists and has not been confirmed. §6: only a verified
    address may receive notification email, because an unverified one may
    belong to somebody who never asked for it."""

    SKIPPED_NO_EMAIL = "skipped_no_email"
    """No account, no address, or an account no longer eligible — deleted,
    deactivated. One outcome for all of them, because the operational answer
    is identical and distinguishing them would report on account state to
    whoever reads the metric."""

    SKIPPED_CHANNEL_UNAVAILABLE = "skipped_channel_unavailable"
    """This process cannot deliver email at all. Reachable when a row was
    enqueued by a node with the channel on and claimed by one with it off —
    the delivery is skipped rather than failed, because nothing about it was
    wrong."""

    RETRYABLE_FAILURE = "retryable_failure"
    """A provider fault that may not recur. The row stays `PENDING` with a
    later `next_attempt_at` until the attempt limit."""

    PERMANENT_FAILURE = "permanent_failure"
    """A provider fault that will recur — a malformed address, a rejected
    sender. Retrying is asking the same question and being told no again."""

    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    """Retried to the limit and never accepted. Distinct from
    `PERMANENT_FAILURE`, because they mean different things to an operator:
    one is a bad address, the other is a provider that was down for hours."""


#: Which outcomes leave a row owed. Everything else is terminal.
_STILL_OWED: Final[frozenset[EmailDeliveryOutcome]] = frozenset(
    {EmailDeliveryOutcome.RETRYABLE_FAILURE}
)

#: What each terminal outcome sets the row's status to. A mapping rather
#: than a chain of `if`s, so an outcome added without a status fails at the
#: lookup instead of defaulting to something plausible.
_STATUS_OF: Final[dict[EmailDeliveryOutcome, EmailDeliveryStatus]] = {
    EmailDeliveryOutcome.DELIVERED: EmailDeliveryStatus.SENT,
    EmailDeliveryOutcome.SKIPPED_PREFERENCE: EmailDeliveryStatus.SKIPPED,
    EmailDeliveryOutcome.SKIPPED_UNSUPPORTED_TYPE: EmailDeliveryStatus.SKIPPED,
    EmailDeliveryOutcome.SKIPPED_UNVERIFIED_EMAIL: EmailDeliveryStatus.SKIPPED,
    EmailDeliveryOutcome.SKIPPED_NO_EMAIL: EmailDeliveryStatus.SKIPPED,
    EmailDeliveryOutcome.SKIPPED_CHANNEL_UNAVAILABLE: EmailDeliveryStatus.SKIPPED,
    EmailDeliveryOutcome.RETRYABLE_FAILURE: EmailDeliveryStatus.PENDING,
    EmailDeliveryOutcome.PERMANENT_FAILURE: EmailDeliveryStatus.FAILED,
    EmailDeliveryOutcome.ATTEMPTS_EXHAUSTED: EmailDeliveryStatus.FAILED,
}


def status_for(outcome: EmailDeliveryOutcome) -> EmailDeliveryStatus:
    return _STATUS_OF[outcome]


def is_retryable(outcome: EmailDeliveryOutcome) -> bool:
    return outcome in _STILL_OWED


def next_attempt_at(
    *,
    now: datetime,
    attempt_count: int,
    base_seconds: int,
    max_seconds: int,
) -> datetime:
    """When a retried delivery becomes due again.

    **Exponential, capped, and deliberately without jitter.** The outbox
    relay's backoff is the same shape and this mirrors it rather than
    inventing a second schedule — an operator reading two retry curves on
    one platform should not have to learn which is which.

    Jitter is what spreads a thundering herd, and there is no herd here: the
    worker claims a bounded batch and sends serially, so two deliveries
    retried at the same instant are already queued behind each other. Adding
    randomness would make the schedule unpredictable for no benefit and
    untestable without injecting a source of it.

    `attempt_count` is the number of attempts **already made**, so the first
    retry waits `base_seconds`.
    """
    delay = min(base_seconds * (2 ** max(attempt_count - 1, 0)), max_seconds)
    return now + timedelta(seconds=delay)


__all__ = [
    "EmailDeliveryOutcome",
    "EmailDeliveryStatus",
    "is_retryable",
    "next_attempt_at",
    "status_for",
]
