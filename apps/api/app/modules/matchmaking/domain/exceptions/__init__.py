"""This module's typed failures, built on the platform hierarchy in
`app.core.exceptions` — never a parallel one.

Inheriting from the existing tree is what makes these work end to end with
no per-module wiring: `app/api/exception_handlers.py` maps by walking an
exception's MRO, so `AlreadyQueued(ConflictError)` already returns `409`
without this module registering a handler.

**None of these carries a wire code of its own**, and that is the rule in
`app.core.error_codes.ErrorCode` applied rather than skipped. A code is
earned only where a client must behave differently and the status plus the
endpoint cannot tell it apart, and each of these is the only failure of its
kind on the endpoint that produces it:

    AlreadyQueued      the one `409` on `POST /matchmaking/queue`
    NotQueued          the one `404` on `GET /matchmaking/queue/me`
    QueueNotPermitted   the one `422` on `POST /matchmaking/queue`

A client reading `409` from the join endpoint knows exactly one thing can
have happened. Adding `already_queued` would grow the enum by a member that
nothing switches on.
"""

from typing import ClassVar

from app.core.error_codes import ErrorCode
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    TemporaryConflictError,
    ValidationError,
)


class AlreadyQueued(ConflictError):
    """The player already holds a live ticket — QT-1.

    `409`, because the platform's state is what refused the request and
    the caller's next move is to leave the queue or wait. One live ticket
    per player **across all pools**: joining `casual` while waiting in
    `ranked` is this error, not a second ticket, because "multi-queueing
    means one player is paired into two simultaneous matches, and one of
    them must be abandoned — which then looks like the opponent's win was
    stolen."

    The service checks before writing to produce this cheaply; the partial
    unique index is what enforces it under concurrency (BE-06), and the
    repository translates that violation into this same type so the two
    paths are indistinguishable to a caller.
    """


class NotQueued(NotFoundError):
    """The player has no live ticket.

    `404` on the read. Deliberately **not** raised by `DELETE
    /matchmaking/queue`, which is idempotent: leaving a queue you are not
    in reaches the state you asked for, and a `404` there would let anybody
    probe their own queue state through the status code of a write.
    """


class TicketNotWaiting(ConflictError):
    """A transition was attempted on a ticket that has already resolved.

    Raised by the aggregate rather than by a service, and it is the guard
    that makes the four states a state *machine* rather than four values —
    a cancelled ticket cannot later expire, and an expired one cannot later
    be matched.

    Reachable through the API only as a race: two devices cancelling at
    once, or a cancel arriving as the expiry sweep commits. The loser is
    told the ticket has already been resolved, which is true.
    """


class QueueNotPermitted(ValidationError):
    """This player may not enter a queue — A64-015.2.

    `422` rather than `409`: nothing about the queue's *state* refused
    this, and retrying will not help until whatever is true of the player
    stops being true.

    **One exception for every cause, and the message names none of them.**
    Today the only cause is a positively recorded offline; the checks
    `QueueEligibilityPolicy` will grow include sanctions and, eventually,
    rules that touch the block graph — and a refusal that varied by cause
    would let a player probe that graph by queueing repeatedly, which is
    exactly what BL-1's "the blocked player is never told" prevents. The
    same reasoning `FriendRequestRecipientUnavailable` applies.

    The *operator* still learns which check fired: the policy logs it with
    the player and the pool before raising.

    Replaces A64-015.1's `QueueNotPermitted`, which named its cause in its
    own class name and so could not survive a second check.
    """


class QueueCooldownActive(TemporaryConflictError):
    """This player declined a match recently and may not queue yet —
    A64-015.5 §3.

    `409` rather than `422`, and beside `AlreadyQueued` rather than under
    `QueueNotPermitted`, for one reason: it is the platform's *state* that
    refused this, it will stop being true on its own, and the caller's next
    move is to wait a stated number of seconds. That is a conflict, and it
    is the same shape `AlreadyQueued` has.

    ## It carries its own wire code, and it names its cause

    Every other queue refusal on this platform deliberately says nothing
    about *why* — see `QueueNotPermitted`, whose docstring argues that a
    refusal varying by cause would let a player probe the block graph by
    queueing repeatedly. That argument does not reach this one, and the
    difference is worth stating rather than treating as an inconsistency:

      - `QueueNotPermitted`'s future causes are facts about **other
        people** — a block, a sanction somebody filed. Naming them leaks.
      - This one is a fact about **the caller's own action, taken seconds
        ago, that they were told about at the time**. There is nothing here
        they do not already know.

    So it is the one refusal that may be explained, and it must be: a client
    that could not distinguish it from `AlreadyQueued` would render "you are
    already in a queue" to somebody who is not, and offer a "leave queue"
    button that does nothing.

    ## `retry_after_seconds`, and what it is not

    The remaining window, floored at zero. It is on the exception rather
    than only in the message so a client can render a countdown without
    parsing prose, and it is **derived from the stored `expires_at`** rather
    than from the configured duration — a cooldown extended by a second
    decline reports its real end, not the length of one decline.

    It deliberately does **not** say how many declines produced it, when
    they happened, or which matches they were. That would be a behavioural
    record on an error response, and the cooldown is a delay rather than a
    disciplinary file — see `QueueCooldown` on why this is not moderation.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.QUEUE_COOLDOWN_ACTIVE
