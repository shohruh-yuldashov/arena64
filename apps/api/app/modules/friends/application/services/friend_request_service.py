"""`FriendRequestService` — the six friend-request use cases.

Orchestrates; does not compute (services.md §3.2). Every business rule is
somewhere else on purpose: the transition rules and the ownership checks are
`FriendRequest`'s, the cross-row rules are `FriendRequestValidator`'s, and
the uniqueness guarantee is the database's. What lives here is the
sequencing, the transaction boundary and the audit log.

## Why the ownership checks are not in this class

A64-013.2 asks for "validate ownership before accept/decline/cancel", and
the obvious place is here — read the request, compare the ids, raise. It is
in `FriendRequest` instead, and the difference matters exactly once: when a
seventh caller appears. A check in a service protects that service's
callers; a check in the aggregate protects everything, including the block
handler A64-013.5 adds and the expiry sweep after it.

This service does not contain the string `addressee_id ==` anywhere, and
that is the property to preserve.

## Transactions

Each write is one unit of work, committed before the method returns.
Resolution is a single-row `UPDATE` guarded by a version, so there is
nothing to coordinate — which stops being true in A64-013.3, where FR-4
requires acceptance and friendship creation to share a transaction. The
`async with self._unit_of_work` below is where that second write goes, and
it is written that way now so adding it is not a restructure.

## Reads open no transaction

`incoming` and `outgoing` are keyset queries over one relation. A unit of
work around a `SELECT` would be ceremony suggesting otherwise — the same
call `ProfileService` makes.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.friends.application.ports import FriendRequestRepository
from app.modules.friends.application.validators import FriendRequestValidator
from app.modules.friends.domain.exceptions import FriendRequestNotFound
from app.modules.friends.domain.friend_request import FriendRequest, FriendRequestStatus

logger = logging.getLogger(__name__)

#: What `GET /friends/requests/{incoming,outgoing}` returns.
#:
#: A tuple rather than a bare `FriendRequestStatus.PENDING`, because the
#: repository takes a sequence and because history is the obvious next
#: filter — A64-013.3's "requests I have responded to" is this constant with
#: more members, not a new method.
_LIVE_ONLY = (FriendRequestStatus.PENDING,)


class FriendRequestService:
    def __init__(
        self,
        *,
        requests: FriendRequestRepository,
        validator: FriendRequestValidator,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._requests = requests
        self._validator = validator
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def send(self, *, requester_id: UUID, addressee_id: UUID) -> FriendRequest:
        """Creates a pending request from `requester_id` to `addressee_id`.

        Raises `SelfFriendRequest` (422), `DuplicateFriendRequest` (409) or
        `OppositeFriendRequestPending` (409) — see `FriendRequestValidator`
        for the rules and the order they run in.

        **Does not check that the addressee exists**, and that is
        deliberate rather than an omission. The foreign key does it, and
        doing it here would mean a read whose *timing* answers "is there an
        account with this id" for any caller — on an endpoint that takes an
        id, that is an existence oracle. A request to a non-existent player
        fails on the constraint, which is the same 404-shaped outcome
        without the probe.
        """
        await self._validator.ensure_can_send(requester_id=requester_id, addressee_id=addressee_id)

        request = FriendRequest.send(
            requester_id=requester_id,
            addressee_id=addressee_id,
            sent_at=self._clock.now(),
        )

        async with self._unit_of_work:
            stored = await self._requests.add(request)
            await self._unit_of_work.commit()

        # **Ids only.** A64-013.2 asks for creation to be logged and forbids
        # personal or profile information; the safe reading is to record no
        # values at all beyond the identifiers. Even so this line is a
        # social-graph edge — who asked whom — which is more sensitive than
        # anything on either profile, so it carries no username, no display
        # name and no message (services.md §8.5).
        logger.info(
            "friend_request_created",
            extra={
                "request_id": str(stored.id),
                "requester_id": str(stored.requester_id),
                "addressee_id": str(stored.addressee_id),
            },
        )
        return stored

    async def accept(self, *, request_id: UUID, actor_id: UUID) -> FriendRequest:
        """The addressee agrees.

        Raises `FriendRequestNotFound` (404), `NotRequestAddressee` (403) or
        `FriendRequestAlreadyResolved` (409).

        **A64-013.3 creates the `Friendship` here**, inside the same
        `async with` (FR-4: "acceptance creates the Friendship in the same
        transaction that resolves the request" — two transactions permit a
        state where the request is accepted and no friendship exists).
        Accepting today resolves the request and nothing else, which is why
        this task ships no friend list.
        """
        return await self._transition(
            request_id=request_id,
            actor_id=actor_id,
            apply=lambda request, at: request.accept(by=actor_id, at=at),
            event="friend_request_accepted",
        )

    async def decline(self, *, request_id: UUID, actor_id: UUID) -> FriendRequest:
        """The addressee refuses. Silent to the requester (FR-3).

        "Silent" is a property of what this platform does *not* do: nothing
        notifies, and the requester sees the request leave their outgoing
        list with no explanation. A notified decline turns a refusal into a
        confrontation.

        The row is kept, because FR-5's future decline cooldown reads it.
        """
        return await self._transition(
            request_id=request_id,
            actor_id=actor_id,
            apply=lambda request, at: request.decline(by=actor_id, at=at),
            event="friend_request_declined",
        )

    async def cancel(self, *, request_id: UUID, actor_id: UUID) -> FriendRequest:
        """The requester withdraws.

        Raises `NotRequestRequester` (403) for anybody else — including the
        addressee, who has `decline`. The two are not interchangeable: they
        leave different history, and FR-5 reads it.

        A `DELETE` on the wire and an `UPDATE` in storage. Nothing is
        removed: database.md §1221 — "a row that ended is a fact with a
        date; the row is history, not debris" — and A64-013.2 says outright
        that accepted rows must survive. The verb describes what the caller
        is doing to their request, not what happens to the row.
        """
        return await self._transition(
            request_id=request_id,
            actor_id=actor_id,
            apply=lambda request, at: request.cancel(by=actor_id, at=at),
            event="friend_request_cancelled",
        )

    async def incoming(
        self, *, addressee_id: UUID, limit: int, cursor: str | None
    ) -> tuple[Sequence[FriendRequest], str | None]:
        """Pending requests this player has received, newest first.

        Read-only; opens no transaction. Returns the page and the next
        cursor — composition into profiles happens above this layer, which
        is what keeps this service free of any dependency on `profiles`.
        """
        return await self._requests.list_for_addressee(
            addressee_id, statuses=_LIVE_ONLY, limit=limit, cursor=cursor
        )

    async def outgoing(
        self, *, requester_id: UUID, limit: int, cursor: str | None
    ) -> tuple[Sequence[FriendRequest], str | None]:
        """Pending requests this player has sent. See `incoming`."""
        return await self._requests.list_for_requester(
            requester_id, statuses=_LIVE_ONLY, limit=limit, cursor=cursor
        )

    async def _transition(
        self,
        *,
        request_id: UUID,
        actor_id: UUID,
        apply: Callable[[FriendRequest, datetime], None],
        event: str,
    ) -> FriendRequest:
        """Read, transition, write, log — the shape all three resolutions
        share.

        The three differ only in which method they call on the aggregate and
        what the log line is called, so they share this rather than
        repeating a read-modify-write with an ownership check in it three
        times. The ownership check is inside `apply`, on the aggregate,
        which is the arrangement this module's docstring is about.

        `FriendRequestNotFound` is raised **before** the actor is
        considered, and the ordering is safe: a caller who invented an id
        learns it does not exist, which tells them nothing about anybody.
        Reversing it — 403 for a request that is not yours, 404 only if it
        genuinely does not exist — would make a guessed id an existence
        oracle.
        """
        request = await self._requests.get(request_id)
        if request is None:
            raise FriendRequestNotFound("No friend request with that identifier.")

        apply(request, self._clock.now())

        async with self._unit_of_work:
            stored = await self._requests.resolve(request)
            await self._unit_of_work.commit()

        # The actor, not both parties. An acceptance already implies who the
        # two are via the request id, and repeating them here would make the
        # log a denormalised social graph rather than an audit trail
        # (services.md §8.5).
        logger.info(
            event,
            extra={"request_id": str(stored.id), "actor_id": str(actor_id)},
        )
        return stored
