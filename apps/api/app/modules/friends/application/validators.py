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
    2. blocked       will be one indexed read; must precede the pending
                     checks so a blocked sender learns nothing about
                     whether a request exists (FR-2)
    3. duplicate     one indexed read
    4. opposite      one indexed read, and the only one whose rejection
                     tells the caller to do something else

Putting the block check after the pending ones would leak: a blocked player
sending twice would get "duplicate" the second time, which confirms their
first request exists — the thing FR-2 exists to hide.
"""

import logging
from uuid import UUID

from app.modules.friends.application.ports import FriendRequestRepository
from app.modules.friends.domain.exceptions import (
    DuplicateFriendRequest,
    OppositeFriendRequestPending,
    SelfFriendRequest,
)

logger = logging.getLogger(__name__)


class FriendRequestValidator:
    """Answers "may this player send this request", and nothing else.

    Holds only the repository. Deliberately not the clock: none of these
    rules is time-dependent today, and FR-5's decline cooldown — which is —
    is not in A64-013.2's scope. When it arrives it takes a `Clock` and this
    class gains a fifth check; that is a constructor change, not a redesign.
    """

    def __init__(self, requests: FriendRequestRepository) -> None:
        self._requests = requests

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
        await self._ensure_not_blocked(requester_id, addressee_id)
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

    async def _ensure_not_blocked(self, requester_id: UUID, addressee_id: UUID) -> None:
        """FR-2 and BL-2 — **the A64-013.5 extension point.**

        A no-op today, and deliberately a *named, called* no-op rather than
        a comment saying where the check will go. The ordering argument in
        this module's docstring only holds if this runs before the pending
        checks, and the cheapest way to guarantee that is for the call to
        already be in the sequence.

        When blocks exist this becomes one read of `friends.block` for
        either direction, and the rejection must be **indistinguishable from
        a request to a player who does not exist** (FR-2) — a distinguishable
        one tells the sender they were blocked, which is exactly what the
        blocker was avoiding. That is why it cannot simply raise a new
        `PlayerBlocked` type: the shape of the refusal is the requirement.

        The parameters are already correct for it, which is the point of
        writing the signature now.
        """
        return None

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
