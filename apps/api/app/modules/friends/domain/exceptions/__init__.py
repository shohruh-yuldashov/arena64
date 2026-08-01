"""This module's typed failures, built on the platform hierarchy in
`app.core.exceptions` — never a parallel one.

Inheriting from the existing tree is what makes these work end to end with
no per-module wiring: `app/api/exception_handlers.py` maps by walking an
exception's MRO, so `FriendRequestNotFound(NotFoundError)` already returns
`404` without this module registering a handler.

**Which of these carry their own wire code.** Per the rule in
`app.core.error_codes.ErrorCode`: a class exists for every distinct failure
because server code branches on the type, but a new *code* is added only
where a client must behave differently and the status plus the endpoint
cannot tell it apart.

Two do. `DuplicateFriendRequest` and `OppositeFriendRequestPending` are both
`409` on the same request, and a client must react differently — the first
means "you already asked", the second means "they asked you, respond to
that instead", and the second is genuinely actionable UI. The rest ride the
generic codes.

## The three ownership failures are deliberately *not* 404

`NotRequestAddressee`, `NotRequestRequester` and `FriendRequestNotFound` are
`403`, `403` and `404` respectively, and the distinction leaks nothing: a
caller who guessed a request id learns only that they are not party to it,
which they knew, and never who is. Collapsing them into one 404 was
considered and rejected — it would make a genuinely missing request
indistinguishable from a permission failure for the *legitimate* party too,
which turns a client bug into an unexplainable one.
"""

from typing import ClassVar

from app.core.error_codes import ErrorCode
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)


class FriendRequestNotFound(NotFoundError):
    """No request with that identifier exists.

    Carries the generic `not_found` code. A caller holding an id they were
    given sees this when the request was hard-deleted — which nothing does,
    since resolution is a status change (database.md §1221: "a row that
    ended is history, not debris") — or when they invented the id.
    """


class SelfFriendRequest(ValidationError):
    """A player addressed a request to themselves.

    A `422` rather than a `409`: nothing about the platform's state made
    this fail, and retrying will not help. The database enforces it too
    (`ck_friend_request__not_self`), so a row cannot exist in this shape
    even if the aggregate were bypassed (BE-06).
    """


class DuplicateFriendRequest(ConflictError):
    """A pending request from this sender to this recipient already exists —
    FR-1.

    `409`, with a code of its own because the client's next move differs
    from every other conflict here: nothing to do, the request is already
    in flight.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.DUPLICATE_FRIEND_REQUEST


class OppositeFriendRequestPending(ConflictError):
    """The *recipient* already has a pending request to the sender.

    A separate type and a separate code from `DuplicateFriendRequest`,
    because the sender's next action is completely different and is
    something a UI should offer: accept the request you already have.

    Not implemented as an automatic mutual-accept, deliberately. Two people
    each sending a request is not the same event as one accepting the
    other's, and silently converting it would resolve a request the
    addressee never acted on — see `FriendRequestValidator`.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.OPPOSITE_FRIEND_REQUEST_PENDING


class FriendRequestAlreadyResolved(ConflictError):
    """The request has already been accepted, declined, cancelled, expired
    or voided.

    `409` and not `404`: the request exists, and the caller's view of it is
    stale rather than wrong. Also what a lost optimistic-concurrency race
    surfaces as — two devices resolving the same request, one of which wins
    (repositories.md §8.4).
    """


class FriendRequestNotPending(ConflictError):
    """A caller needed the request to be pending and it is not.

    Distinct from `FriendRequestAlreadyResolved` by *who is asking*: that
    one is a party attempting a transition, this one is a caller inspecting
    state before doing something else. A64-013.5's block handler is the
    consumer.
    """


class InvalidFriendsCursor(ValidationError):
    """The pagination cursor is malformed — A64-013.2.

    A `422` rather than an empty page: a corrupted cursor is malformed
    input, and silently restarting from the first page would make a client
    loop forever without noticing.

    Shared by the request lists and the friend list, because they share a
    cursor: both are keyset pages over `(created_at, id)` on a relation
    scoped to the authenticated caller, so there is one encoding and one way
    for it to be wrong. A64-013.3 renamed it from
    `InvalidFriendRequestCursor` when the second list arrived.
    """


class SelfFriendship(ValidationError):
    """A player was paired with themselves — A64-013.3.

    Unreachable through the request flow, which refuses a self-request in
    the aggregate, in the validator and in `ck_friend_request__not_self`.
    This is the guard for the aggregate that would have to be wrong for it
    to happen anyway, and for any future path that creates a friendship
    without a request.
    """


class FriendshipAlreadyExists(ConflictError):
    """The two players already have a live friendship — A64-013.3.

    `409`. Reachable in practice only by two acceptances racing, since the
    request flow refuses a second pending request for a pair (FR-1); the
    partial unique index is what makes it impossible rather than unlikely.
    """


class FriendshipNotFound(NotFoundError):
    """No live friendship exists between the two players.

    `404`, and deliberately the same answer whether the two were never
    friends or the friendship has already ended. The distinction is not a
    caller's to learn: "are these two people friends" is exactly what
    `VisibilityLevel.FRIENDS` exists to control, and an endpoint that
    answered it differently for the two cases would be a way to ask.
    """


class FriendshipAlreadyEnded(ConflictError):
    """The friendship is no longer live.

    `409` and not `404`: the row exists and the caller's view of it is
    stale rather than wrong — the same distinction `FriendRequestAlreadyResolved`
    draws, and reachable the same way, by two devices removing at once.
    """


class NotFriendshipParticipant(PermissionDeniedError):
    """Somebody who is not one of the two tried to act on a friendship.

    `403`. The message names neither participant — a rejection that did
    would turn a guessed identifier into a way to learn who is friends with
    whom.
    """


class NotRequestAddressee(PermissionDeniedError):
    """Somebody other than the recipient tried to accept or decline.

    `403`. The message never names the other party — a rejection that did
    would turn a guessed identifier into a way to learn who is sending
    requests to whom.
    """


class NotRequestRequester(PermissionDeniedError):
    """Somebody other than the sender tried to cancel.

    Includes the *addressee*, who has `decline` for the same practical
    outcome. The two are kept separate because they leave different history,
    and FR-5's decline cooldown reads it.
    """


__all__ = [
    "DuplicateFriendRequest",
    "FriendRequestAlreadyResolved",
    "FriendRequestNotFound",
    "FriendRequestNotPending",
    "FriendshipAlreadyEnded",
    "FriendshipAlreadyExists",
    "FriendshipNotFound",
    "InvalidFriendsCursor",
    "NotFriendshipParticipant",
    "SelfFriendship",
    "NotRequestAddressee",
    "NotRequestRequester",
    "OppositeFriendRequestPending",
    "SelfFriendRequest",
]
