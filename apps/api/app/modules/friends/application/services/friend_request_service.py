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

## Transactions, and the one that carries two writes

Each write is one unit of work, committed before the method returns.

**Acceptance is the exception, and it is the point of A64-013.3.** FR-4:
"acceptance creates the `Friendship` in the same transaction that resolves
the request — two transactions permit a state where the request is accepted
and no friendship exists." So `accept` resolves the request *and* creates
the friendship inside one `async with self._unit_of_work`, with one
`commit()` at the end.

The failure this prevents is not hypothetical and is not self-correcting: a
request that says `accepted` with no friendship row is a pair who believe
they are friends and are not, and nothing in the system would ever notice —
there is no reconciliation pass, because the accepted request is a
perfectly valid terminal state on its own.

`_transition` takes an optional `on_resolved` hook that runs **inside** that
block, which is how the second write gets there without `accept` growing its
own copy of the read-transition-write-log shape.

## Reads open no transaction

`incoming` and `outgoing` are keyset queries over one relation. A unit of
work around a `SELECT` would be ceremony suggesting otherwise — the same
call `ProfileService` makes.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.friends.application.ports import (
    FriendRequestRepository,
    FriendshipRepository,
    SocialGraphCache,
)
from app.modules.friends.application.validators import FriendRequestValidator
from app.modules.friends.domain.events import FriendRequestAccepted, FriendRequestSent
from app.modules.friends.domain.exceptions import FriendRequestNotFound
from app.modules.friends.domain.friend_request import FriendRequest, FriendRequestStatus
from app.modules.friends.domain.friendship import Friendship
from app.platform.outbox import EventPublisher

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
        friendships: FriendshipRepository,
        cache: SocialGraphCache,
        events: EventPublisher,
        validator: FriendRequestValidator,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._requests = requests
        # Held so that `accept` can write the friendship inside the unit of
        # work it already opens (FR-4). Deliberately the *repository* rather
        # than `FriendshipService`: that service opens transactions of its
        # own, and calling it from here would produce the nested,
        # two-transaction shape A64-013.3 forbids. What is needed here is a
        # write that joins the caller's transaction, which is exactly what a
        # repository is.
        self._friendships = friendships
        # The fourth `friends:v1:` invalidation trigger — acceptance is the
        # only way a friendship comes into existence, so it is the only way
        # a cached friend set can gain a member.
        self._cache = cache
        # A64-013.7. Published inside `_create_friendship`, which is inside
        # the acceptance's transaction — so the request, the friendship and
        # the event are one fact (FR-4 and AD-16 agreeing).
        self._events = events
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
            # A64-021.1. The second write in this transaction, and it is
            # here rather than after the commit for AD-16's reason: a
            # request that committed without its event would be a request
            # the addressee is never told about, with nothing recording that
            # a notification was owed.
            #
            # The request's own `created_at` rather than a second
            # `clock.now()`: the request was sent once, and two readings of
            # the clock would be two answers to one question.
            await self._events.publish(
                FriendRequestSent(
                    occurred_at=stored.created_at,
                    request_id=stored.id,
                    requester_id=stored.requester_id,
                    addressee_id=stored.addressee_id,
                )
            )
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
        """The addressee agrees, **and the friendship is created with it**.

        Raises `FriendRequestNotFound` (404), `NotRequestAddressee` (403) or
        `FriendRequestAlreadyResolved` (409).

        ## One transaction, two writes — FR-4

        The resolved request and the new `Friendship` are written inside the
        same unit of work and committed once. Anything else permits a state
        where the request says `accepted` and no friendship exists, which is
        a pair who believe they are friends and are not — and which nothing
        would ever notice, because an accepted request is a valid terminal
        state on its own.

        The friendship's `created_at` is the *same instant* as the request's
        `responded_at`, because both come from one `self._clock.now()` in
        `_transition`. They are one event.

        `FriendshipAlreadyExists` (409) is possible in principle — two
        acceptances racing — and is what the partial unique index refuses.
        In practice FR-1 makes it hard to reach, since a pair cannot have
        two pending requests; the constraint is what makes it impossible
        rather than unlikely.
        """
        return await self._transition(
            request_id=request_id,
            actor_id=actor_id,
            apply=lambda request, at: request.accept(by=actor_id, at=at),
            event="friend_request_accepted",
            on_resolved=self._create_friendship,
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

    async def _create_friendship(self, request: FriendRequest, at: datetime) -> None:
        """Creates the friendship an acceptance implies.

        Called by `_transition` **inside** the unit of work, after the
        request has been resolved and before the commit — which is the whole
        of FR-4. It is a method rather than a lambda so the transaction
        boundary it depends on is documented where it runs.

        `Friendship.between` sorts the pair, so this passes requester and
        addressee in whatever order the request happened to have and never
        has to know about `low` and `high` (DB-12).

        `at` is the instant the request was resolved, threaded through
        rather than read again: the friendship began when the request was
        accepted, and two `clock.now()` calls would record two answers to
        one question.
        """
        friendship = await self._friendships.create(
            Friendship.between(
                request.requester_id,
                request.addressee_id,
                created_at=at,
                source_request_id=request.id,
            )
        )

        # A64-013.7. The third write in this transaction, and the reason
        # there is no fourth anywhere else: an acceptance that committed
        # without its event would be a friendship nobody is ever told about,
        # with nothing recording that a notification was owed (AD-16).
        await self._events.publish(
            FriendRequestAccepted(
                occurred_at=at,
                request_id=request.id,
                requester_id=request.requester_id,
                addressee_id=request.addressee_id,
                friendship_id=friendship.id,
            )
        )

        # Ids only, and both parties — unlike the removal log, which names
        # one. A friendship is mutual and both sides consented to it, so
        # recording who is now friends with whom is an audit fact rather
        # than a disclosure about somebody's choices (contrast
        # `friendship_removed`, where FS-2's silence applies).
        logger.info(
            "friendship_created",
            extra={
                "request_id": str(request.id),
                "requester_id": str(request.requester_id),
                "addressee_id": str(request.addressee_id),
            },
        )

    async def _transition(
        self,
        *,
        request_id: UUID,
        actor_id: UUID,
        apply: Callable[[FriendRequest, datetime], None],
        event: str,
        on_resolved: Callable[[FriendRequest, datetime], Awaitable[None]] | None = None,
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

        # One instant for the whole resolution. `responded_at` and, on the
        # acceptance path, the friendship's `created_at` are the same event
        # and must carry the same timestamp — two `now()` calls would record
        # two answers to one question.
        at = self._clock.now()
        apply(request, at)

        async with self._unit_of_work:
            stored = await self._requests.resolve(request)
            if on_resolved is not None:
                # **Inside** the unit of work, after the resolution and
                # before the commit — FR-4. Moving this one line below the
                # `async with` is the bug A64-013.3 exists to prevent, which
                # is why the hook is invoked here rather than by `accept`
                # after it returns.
                await on_resolved(stored, at)
            await self._unit_of_work.commit()

        if on_resolved is not None:
            # A64-013.6. **Outside** the unit of work, unlike the write
            # above and for the opposite reason: a cache dropped before the
            # commit can be repopulated from the pre-commit state by a
            # concurrent read, and nothing would invalidate it again. Only
            # acceptance changes the graph — declining and cancelling leave
            # every friend set exactly as it was — so this is guarded by the
            # same condition the friendship write is.
            await self._cache.invalidate((stored.requester_id, stored.addressee_id))

        # The actor, not both parties. An acceptance already implies who the
        # two are via the request id, and repeating them here would make the
        # log a denormalised social graph rather than an audit trail
        # (services.md §8.5).
        logger.info(
            event,
            extra={"request_id": str(stored.id), "actor_id": str(actor_id)},
        )
        return stored
