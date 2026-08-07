"""Sending and answering a friend challenge — A64-022.1 §3, §7, §16.

Four use cases: create, decline, cancel, expire. **No `accept`** — see the
aggregate's module docstring, and §17 below.

## Who may challenge whom, and where that is decided

Here, on the server, from the modules that own the answers:

    friendship   `friends.public.SocialGraphReader.friend_ids_among`
    blocking     `friends.public.PairingExclusions.blocked_pairs_among`
    the clock    `reference.public.TimeControlCatalogue.active`

None of it is re-derived and none of it is trusted from a client. A frontend
that decided somebody was a friend would be a frontend that could challenge
a stranger by lying.

## Why blocking and non-friendship are the same answer

`domain-model.md` §10.3, BL-2 and FR-2: *"a challenge to a blocked player
fails indistinguishably"*. A blocked player must not learn they were
blocked, so "you are not friends" is the only answer either case can give —
and since a block also ends a friendship, the sentence is true in both.

That is why there is no `challenge_blocked` error. An error code that
existed would be the disclosure, whatever the message beside it said.

## What this service does not do

It publishes no events yet. `FriendChallengeCreated` and its three siblings
exist (`domain/challenge_events.py`) and nothing writes them to the outbox,
because their consumers are A64-022.2's realtime frame and notification —
and an event with no consumer that also has no producer is the honest state
for a phase that built neither. Wiring the publisher is one constructor
argument, and it belongs with the first consumer.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.exceptions import NotFoundError, RuleViolationError
from app.core.identifiers import generate_uuid7
from app.core.unit_of_work import UnitOfWork
from app.modules.friends.public import PairingExclusions, SocialGraphReader
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.application.ports import ChallengeRepository
from app.modules.matchmaking.domain.challenge import (
    Challenge,
    ChallengeSelfNotAllowed,
    issue,
)
from app.modules.reference.public import TimeControlCatalogue, TimeControlId

logger = logging.getLogger(__name__)


class ChallengeNotFriends(RuleViolationError):
    """The two are not friends, **or** one has blocked the other.

    One error for both, deliberately — see the module docstring. A separate
    "blocked" code would tell somebody they had been blocked, which is the
    one thing BL-1 withholds.
    """


class ChallengeInvalidTimeControl(RuleViolationError):
    """The requested clock is not one the platform currently offers.

    Raised for an identifier that has been retired as well as one that never
    existed: a control removed from the catalogue must not be choosable for a
    new game, while rows that already reference it stay readable.
    """


class ChallengeService:
    """The four challenge use cases, over one unit of work."""

    def __init__(
        self,
        *,
        challenges: ChallengeRepository,
        social_graph: SocialGraphReader,
        exclusions: PairingExclusions,
        time_controls: TimeControlCatalogue,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._challenges = challenges
        self._social_graph = social_graph
        self._exclusions = exclusions
        self._time_controls = time_controls
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def create(
        self,
        challenger_id: UUID,
        *,
        recipient_id: UUID,
        time_control_id: TimeControlId,
        variant: ProductVariant,
        rated: bool = False,
    ) -> Challenge:
        """Invites one friend to play.

        `challenger_id` comes from the session at the call site and is the
        only way it can arrive — there is no parameter a transport could fill
        from a body (§21).

        ## The order of the checks

        Self, then the clock, then the relationship. Cheapest and most final
        first, and the last one is last because it is the only one that costs
        two reads — a caller who named themselves or a clock that does not
        exist is refused before the social graph is touched at all.

        `rated` defaults to `False` and this phase can produce nothing else:
        Arena64 requires **both** players to agree before a direct game
        affects ratings, and the recipient's half of that agreement lives at
        acceptance, which A64-022.3 owns. A `rated=True` challenge is
        therefore a request that nothing can currently grant, which is why
        the parameter exists and the default does not.
        """
        # **Self first**, before any reader is touched. `issue` refuses it
        # too — it is the one rule that needs nothing but the two ids — but
        # reaching that would mean asking the social graph first, and a
        # player is not their own friend: the caller would be told they are
        # not friends with themselves, which is true and useless.
        if challenger_id == recipient_id:
            raise ChallengeSelfNotAllowed("a player cannot challenge themselves")

        await self._require_offerable(time_control_id)
        await self._require_friends(challenger_id, recipient_id)

        challenge = issue(
            challenge_id=generate_uuid7(),
            challenger_id=challenger_id,
            recipient_id=recipient_id,
            time_control_id=time_control_id,
            variant=variant,
            rated=rated,
            at=self._clock.now(),
        )

        async with self._unit_of_work:
            # May raise `ConflictError` — the pair already has a live one.
            # Not checked first: `find_live_between` would lose the race
            # between two friends challenging each other at the same moment,
            # which is exactly the pair the rule is about.
            await self._challenges.add(challenge)
            await self._unit_of_work.commit()

        # Ids only. No player names, no settings — a log line that carried
        # who challenged whom by name would put the social graph in a log
        # aggregator.
        logger.info(
            "friend_challenge_created",
            extra={"challenge_id": str(challenge.id), "rated": challenge.rated},
        )
        return challenge

    async def decline(self, challenge_id: UUID, *, by: UUID) -> Challenge:
        """The recipient says no."""
        return await self._settle(challenge_id, by=by, transition="decline")

    async def cancel(self, challenge_id: UUID, *, by: UUID) -> Challenge:
        """The challenger withdraws it."""
        return await self._settle(challenge_id, by=by, transition="cancel")

    async def expire(self, challenge_id: UUID, *, by: UUID) -> Challenge:
        """Settles one challenge whose window has closed.

        Takes a `by` like the other two and uses it only to **read**: expiry
        is the platform's transition and has no actor, but the row still has
        to be fetched, and this service has exactly one scoped read.

        A sweep does not go through here — it will claim rows in bulk
        (A64-022.6). This exists so that a challenge found expired on a read
        path can be settled rather than left to look pending until the sweep
        catches up.
        """
        return await self._settle(challenge_id, by=by, transition="expire")

    async def _settle(self, challenge_id: UUID, *, by: UUID, transition: str) -> Challenge:
        """The shared half of the three terminal transitions.

        One read, one aggregate call, one guarded write — and the aggregate
        decides whether this actor may make this transition, so the authority
        question is answered in the framework-free layer rather than here.

        The whole thing is inside the unit of work: the read that produced
        the aggregate and the write that settles it must see the same row, or
        two callers could both read `PENDING` and both write. The repository
        guards on `status = 'pending'` for the case where they do anyway.
        """
        now = self._clock.now()
        async with self._unit_of_work:
            challenge = await self._challenges.get_for_party(challenge_id, party_id=by)
            if challenge is None:
                # Not found for a challenge that does not exist **and** for
                # one between two other people — an id that answered
                # differently would be an existence oracle (§21).
                raise NotFoundError("No such challenge.")

            settled = (
                challenge.expire(at=now)
                if transition == "expire"
                else challenge.decline(by=by, at=now)
                if transition == "decline"
                else challenge.cancel(by=by, at=now)
            )
            await self._challenges.save(settled)
            await self._unit_of_work.commit()

        logger.info(
            "friend_challenge_settled",
            extra={"challenge_id": str(settled.id), "status": settled.status.value},
        )
        return settled

    async def _require_offerable(self, time_control_id: TimeControlId) -> None:
        """The clock must be one the platform currently offers.

        Asked of `reference`, which owns the catalogue, rather than by
        checking the enum: a member of `TimeControlId` may be *retired*, and
        an enum check would keep offering it forever. `active()` is the same
        read the queue uses, so the two entry points cannot disagree about
        what is on the menu.
        """
        offered = {control.id for control in await self._time_controls.active()}
        if time_control_id not in offered:
            raise ChallengeInvalidTimeControl("That time control is not available.")

    async def _require_friends(self, challenger_id: UUID, recipient_id: UUID) -> None:
        """Friends, and neither has blocked the other.

        Two batch reads of one element each. The singular shape is what this
        use case needs — one challenge names one person — and the ports are
        batch-only, so the cost is the same query with a one-element `IN`.

        The block check runs even though a block ends a friendship, because
        the two facts are stored separately and this is the boundary that
        must not let a stale friendship row through.
        """
        friends: set[UUID] = await self._social_graph.friend_ids_among(
            challenger_id, [recipient_id]
        )
        if recipient_id not in friends:
            raise ChallengeNotFriends("You can only challenge your friends.")

        # `blocked_pairs_among` answers "who in this batch must this player
        # not be paired with", and it is **symmetric** — so asking about the
        # challenger is enough and the reverse direction is already folded
        # into the answer. Checking both would be checking one fact twice.
        blocked = await self._exclusions.blocked_pairs_among([challenger_id, recipient_id])
        if recipient_id in blocked.get(challenger_id, frozenset()):
            # The same error and the same sentence as "not friends" — see the
            # module docstring on why this must be indistinguishable.
            raise ChallengeNotFriends("You can only challenge your friends.")


__all__ = ["ChallengeInvalidTimeControl", "ChallengeNotFriends", "ChallengeService"]
