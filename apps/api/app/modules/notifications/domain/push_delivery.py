"""One attempt to push a notification to one device — A64-021.6 §17, §18, §19.

Framework-free. The state machine, the bounded vocabulary of *why* a
delivery stopped, and the backoff that decides when a retry is due.

## Why this is a separate file from `email_delivery`

The two are close enough that sharing was considered and rejected. They
differ in the one place that matters — **what a row is**.

    email    one row per notification. A person has one address
    push     one row per notification **per device**. A person has a laptop,
             a phone and a second browser, and each is a separate delivery
             that succeeds or fails on its own

That difference propagates into the key, the claim query, the fan-out and
the cleanup. A shared abstraction over it would be parameterised by the
thing that actually varies, which is the definition of the wrong
abstraction — CLAUDE.md §2.7: duplication is cheaper.

What *is* shared is the shape: the same status vocabulary, the same
"outcome is returned, not raised" rule, and the same exponential backoff
(§18 — do not invent a second scheduler), so an operator reading two retry
curves on one platform does not have to learn which is which.

## Why one dead device does not fail the others

§9. Each device's row is claimed, attempted and settled independently. A
`410` from one push service revokes one subscription and touches nothing
else, which is only true because the row is per device.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final


class PushDeliveryStatus(StrEnum):
    """Where a delivery row currently stands.

    The same four as email, deliberately. `PENDING` covers "never tried" and
    "will try again" for the reason its twin gives: they are the same
    question to the worker — *is this due* — and splitting them would give
    the claim query a second predicate that could disagree with
    `next_attempt_at`.
    """

    PENDING = "pending"
    """Owed. `next_attempt_at` says when."""

    SENT = "sent"
    """A push service accepted it. **Not** "the person saw it" — the device
    may be asleep, the browser closed, the notification dismissed unread.
    Nothing downstream of this platform reports back, which is why there is
    no `DELIVERED` and no read receipt."""

    SKIPPED = "skipped"
    """Deliberately not sent. `outcome` says why, and every reason is one
    the platform decided rather than one a push service reported."""

    FAILED = "failed"
    """Abandoned. A permanent rejection or the attempt limit; `outcome`
    distinguishes them."""


class PushDeliveryOutcome(StrEnum):
    """Why a delivery ended where it did — the bounded vocabulary §26 needs.

    Every member is a **label this platform chose**, never a push service's
    string. That is what keeps it safe on a metric: a vendor error code as a
    label is an unbounded cardinality dimension fed by a third party.
    """

    DELIVERED = "delivered"
    """Handed to a push service, which accepted it."""

    SKIPPED_PREFERENCE = "skipped_preference"
    """The recipient muted this category on push."""

    SKIPPED_UNSUPPORTED_TYPE = "skipped_unsupported_type"
    """Not a type this platform pushes — see `domain.push.PUSH_CAPABLE_TYPES`.
    Reachable when a type is *removed* from that set while deliveries for it
    are already queued."""

    SKIPPED_NO_SUBSCRIPTION = "skipped_no_subscription"
    """The device's subscription was revoked between enqueue and send — the
    person signed out on it, or an earlier delivery found it gone. Nothing
    was wrong; there is simply nowhere to send."""

    SKIPPED_CHANNEL_UNAVAILABLE = "skipped_channel_unavailable"
    """This process holds no VAPID key pair. Reachable when a row was
    enqueued by a node with push configured and claimed by one without —
    skipped rather than failed, because nothing about it was wrong."""

    SUBSCRIPTION_GONE = "subscription_gone"
    """The push service answered `404`/`410`. The browser is gone: cleared
    site data, uninstalled the PWA, revoked the permission.

    Distinct from `PERMANENT_FAILURE` because it is not a failure at all —
    it is the ordinary end of a subscription's life, it is the **only**
    outcome that revokes one, and an operator seeing a spike of these is
    seeing people churn devices rather than a platform fault.
    """

    RETRYABLE_FAILURE = "retryable_failure"
    """A push service fault that may not recur. The row stays `PENDING` with
    a later `next_attempt_at` until the attempt limit."""

    PERMANENT_FAILURE = "permanent_failure"
    """A rejection that will recur — a stored key that cannot encrypt, an
    assertion the service will not take. Retrying is asking the same
    question and being told no again."""

    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    """Retried to the limit and never accepted. Distinct from
    `PERMANENT_FAILURE` because they mean different things: one is a broken
    subscription, the other is a push service that was down for hours."""


#: Which outcomes leave a row owed. Everything else is terminal.
_STILL_OWED: Final[frozenset[PushDeliveryOutcome]] = frozenset(
    {PushDeliveryOutcome.RETRYABLE_FAILURE}
)

#: Which outcomes mean the *subscription* is finished, not just this message.
#:
#: Exactly one, and keeping it a set rather than an `== SUBSCRIPTION_GONE`
#: is what makes the revocation rule readable at the call site: the worker
#: asks "does this outcome kill the device" rather than comparing an enum.
_REVOKES_SUBSCRIPTION: Final[frozenset[PushDeliveryOutcome]] = frozenset(
    {PushDeliveryOutcome.SUBSCRIPTION_GONE}
)

#: What each outcome sets the row's status to. A mapping rather than a chain
#: of `if`s, so an outcome added without a status fails at the lookup instead
#: of defaulting to something plausible.
_STATUS_OF: Final[dict[PushDeliveryOutcome, PushDeliveryStatus]] = {
    PushDeliveryOutcome.DELIVERED: PushDeliveryStatus.SENT,
    PushDeliveryOutcome.SKIPPED_PREFERENCE: PushDeliveryStatus.SKIPPED,
    PushDeliveryOutcome.SKIPPED_UNSUPPORTED_TYPE: PushDeliveryStatus.SKIPPED,
    PushDeliveryOutcome.SKIPPED_NO_SUBSCRIPTION: PushDeliveryStatus.SKIPPED,
    PushDeliveryOutcome.SKIPPED_CHANNEL_UNAVAILABLE: PushDeliveryStatus.SKIPPED,
    PushDeliveryOutcome.SUBSCRIPTION_GONE: PushDeliveryStatus.FAILED,
    PushDeliveryOutcome.RETRYABLE_FAILURE: PushDeliveryStatus.PENDING,
    PushDeliveryOutcome.PERMANENT_FAILURE: PushDeliveryStatus.FAILED,
    PushDeliveryOutcome.ATTEMPTS_EXHAUSTED: PushDeliveryStatus.FAILED,
}


def status_for(outcome: PushDeliveryOutcome) -> PushDeliveryStatus:
    return _STATUS_OF[outcome]


def is_retryable(outcome: PushDeliveryOutcome) -> bool:
    return outcome in _STILL_OWED


def revokes_subscription(outcome: PushDeliveryOutcome) -> bool:
    """Whether this outcome means the device is finished, not just this send."""
    return outcome in _REVOKES_SUBSCRIPTION


def next_attempt_at(
    *,
    now: datetime,
    attempt_count: int,
    base_seconds: int,
    max_seconds: int,
) -> datetime:
    """When a retried delivery becomes due again.

    Exponential and capped, identical to the email channel's — §18 forbids a
    second scheduler and this is the same shape rather than a new one.
    Without jitter for the same reason: the worker claims a bounded batch
    and sends serially, so two deliveries retried at the same instant are
    already queued behind each other.

    `attempt_count` is the number of attempts **already made**, so the first
    retry waits `base_seconds`.
    """
    delay = min(base_seconds * (2 ** max(attempt_count - 1, 0)), max_seconds)
    return now + timedelta(seconds=delay)


__all__ = [
    "PushDeliveryOutcome",
    "PushDeliveryStatus",
    "is_retryable",
    "next_attempt_at",
    "revokes_subscription",
    "status_for",
]
