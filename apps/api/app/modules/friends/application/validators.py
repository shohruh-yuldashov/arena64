"""`FriendRequestValidator` — the rules that need to see rows other than
the one being written.

Application layer rather than `domain/`, and the split is precise: a rule
belongs in the aggregate when the aggregate can see everything it needs.
"Not to yourself" is such a rule and lives in `FriendRequest.send`. "Not if
one is already pending" is not — it is a statement about the *relation*, and
answering it requires a repository, which an aggregate must never hold.

## Why a class of its own rather than four lines in the service

Because of what A64-013.5 adds. The block rules (FR-2, BL-2) are two more
checks of exactly this shape — look at another relation, refuse — and they
have to run in the same place, in a defined order, before a request is
created. A service method that grew them inline would be a use case with a
policy embedded in it; a validator is the seam where the next two rules are
one method each.

The extension point is `_ensure_not_blocked` below: named, called, and
documented as a no-op that A64-013.5 fills.

## Ordering is part of the contract

The checks run cheapest-first and most-informative-last, and both matter:

    1. self          no I/O at all
    2. reachable     two indexed reads — blocked pair, then existence. Must
                     precede the pending checks so a blocked sender learns
                     nothing about whether a request exists (FR-2)
    3. duplicate     one indexed read
    4. opposite      one indexed read, and the only one whose rejection
                     tells the caller to do something else

Putting the reachability check after the pending ones would leak: a blocked
player sending twice would get "duplicate" the second time, which confirms
their first request exists — the thing FR-2 exists to hide.
"""

import logging
from uuid import UUID

from app.modules.friends.application.ports import (
    BlockedPlayerRepository,
    FriendRequestRepository,
)
from app.modules.friends.domain.exceptions import (
    DuplicateFriendRequest,
    FriendRequestRecipientUnavailable,
    OppositeFriendRequestPending,
    SelfFriendRequest,
)
from app.modules.users.public import PublicProfileReader

#: One message for two causes — see `_ensure_recipient_reachable`. A single
#: constant rather than two identical literals, because two would eventually
#: be edited apart and the difference would be a disclosure.
_UNREACHABLE_RECIPIENT = "That player cannot receive friend requests."

logger = logging.getLogger(__name__)


class FriendRequestValidator:
    """Answers "may this player send this request", and nothing else.

    Holds only the repository. Deliberately not the clock: none of these
    rules is time-dependent today, and FR-5's decline cooldown — which is —
    is not in A64-013.2's scope. When it arrives it takes a `Clock` and this
    class gains a fifth check; that is a constructor change, not a redesign.
    """

    def __init__(
        self,
        requests: FriendRequestRepository,
        *,
        blocks: BlockedPlayerRepository,
        players: PublicProfileReader,
    ) -> None:
        self._requests = requests
        self._blocks = blocks
        # `users`' published port, for the existence half of FR-2. The
        # narrowest thing that answers "does this account exist and is it
        # visible" — it returns `PublicUserProfile`, which has no email
        # field, so this validator cannot leak an address even in principle.
        self._players = players

    async def ensure_can_send(self, *, requester_id: UUID, addressee_id: UUID) -> None:
        """Raises the first rule this pair violates, or returns.

        Returns `None` rather than a result object: every failure is a typed
        exception the platform handler already maps, and a caller that had
        to inspect a result could forget to. The only correct use of this
        method is to call it and let it raise.

        **Not atomic with the write, and does not need to be.** Two
        concurrent sends can both pass here; the partial unique index
        refuses the second, and the repository translates that into the same
        `DuplicateFriendRequest` this class raises (BE-06). The check exists
        to produce a good error cheaply, not to be the guard.
        """
        self._ensure_not_self(requester_id, addressee_id)
        await self._ensure_recipient_reachable(requester_id, addressee_id)
        await self._ensure_no_duplicate(requester_id, addressee_id)
        await self._ensure_no_opposite(requester_id, addressee_id)

    @staticmethod
    def _ensure_not_self(requester_id: UUID, addressee_id: UUID) -> None:
        """FR: a player cannot befriend themselves.

        Checked here *as well as* in `FriendRequest.send` and *as well as*
        in the database's `ck_friend_request__not_self`. Three copies of one
        rule, and each has a different job: this one produces the error
        before any I/O, the aggregate's makes the invalid object
        unconstructible, and the constraint makes the invalid row
        unwritable. BE-06 is explicit that the constraint is the
        authoritative one.
        """
        if requester_id == addressee_id:
            raise SelfFriendRequest("A player cannot send a friend request to themselves.")

    async def _ensure_recipient_reachable(self, requester_id: UUID, addressee_id: UUID) -> None:
        """FR-2 and BL-2 — **filled in by A64-013.5.**

        Two failures, **one exception, one message**, and that is the whole
        requirement rather than an economy:

            blocked pair       either party has blocked the other
            no such player     the id belongs to nobody, or to a
                               deactivated account

        FR-2: "a request to a blocked or blocking player is rejected —
        indistinguishably from a request to a non-existent player.
        Distinguishable rejection tells the sender they were blocked, which
        is exactly what the blocker was avoiding." A `PlayerBlocked` type
        would have satisfied the letter of "reject" and defeated the point.

        ## Why the existence check is here now and was not before

        A64-013.2 deliberately did *not* check that the addressee exists,
        on the grounds that a read whose timing answers "is there an account
        with this id" is an existence oracle on an endpoint taking an id.

        That argument does not survive blocking: this method now performs a
        read regardless, so the timing signal exists either way — and
        without the existence check the two rejections would be
        distinguishable by *outcome*, which is worse. A request to a
        non-existent player used to succeed and create an inert row; it now
        fails exactly as a blocked one does.

        Ordered blocked-first so a blocked sender never reaches a query
        about the addressee's account.
        """
        blocked = await self._blocks.blocked_ids_for(requester_id)
        if addressee_id in blocked:
            raise FriendRequestRecipientUnavailable(_UNREACHABLE_RECIPIENT)

        recipients = await self._players.find_public_profiles([addressee_id])
        if addressee_id not in recipients:
            # `find_public_profiles` omits deactivated accounts, so a
            # withdrawn player is unreachable here for the same reason they
            # have no public profile — and reports it the same way.
            raise FriendRequestRecipientUnavailable(_UNREACHABLE_RECIPIENT)

    async def _ensure_no_duplicate(self, requester_id: UUID, addressee_id: UUID) -> None:
        """FR-1: at most one pending request per *ordered* pair.

        "Otherwise 'send request' becomes a harassment primitive" —
        domain-model.md's reasoning, and the reason this is a rule rather
        than an idempotent no-op. Re-sending must fail loudly, not silently
        succeed against the existing row.
        """
        existing = await self._requests.find_pending_between(requester_id, addressee_id)
        if existing is not None:
            raise DuplicateFriendRequest("You already have a pending request to that player.")

    async def _ensure_no_opposite(self, requester_id: UUID, addressee_id: UUID) -> None:
        """The reverse direction already has one pending.

        **Refused rather than auto-accepted**, which is the decision worth
        recording because auto-accepting is the obvious alternative and is
        wrong. Two people each sending a request is not the same event as
        one accepting the other's: nobody has agreed to anything, and
        converting it would resolve a request the addressee never acted on
        and create a friendship from two unilateral acts.

        The rejection carries its own code so a client can offer the right
        next step — *accept the request you already have* — which is real,
        actionable UI and the whole reason this is not folded into
        `DuplicateFriendRequest`.
        """
        opposite = await self._requests.find_pending_between(addressee_id, requester_id)
        if opposite is not None:
            raise OppositeFriendRequestPending(
                "That player has already sent you a friend request. Respond to it instead."
            )
