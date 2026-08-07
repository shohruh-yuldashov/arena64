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

## Events — A64-022.2

Three of the four are published here, each **inside the transaction that
made the fact true** (AD-16). A challenge that committed without its event
would be an invitation nobody is told about, with nothing recording that a
notification was owed.

`occurred_at` is the aggregate's own timestamp rather than a second clock
read: the thing happened once, and two readings would be two answers to one
question.

`FriendChallengeExpired` is published by the sweep that writes the terminal
row (A64-022.6), not here. `expire` exists on this service for a read path
that finds a stale row, and a phase that published from both would emit the
event twice for one challenge.

`FriendChallengeAccepted` does not exist — see the aggregate on why.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import ClassVar, Final
from uuid import UUID

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.exceptions import NotFoundError, RuleViolationError
from app.core.identifiers import generate_uuid7
from app.core.unit_of_work import UnitOfWork
from app.modules.friends.public import PairingExclusions, SocialGraphReader
from app.modules.game.public import (
    AcceptancePolicy,
    CreateMatchRequest,
    MatchCreationUseCase,
    MatchOrigin,
    MatchParticipant,
    MatchTimeControl,
    ProductVariant,
    game_engine_version,
)
from app.modules.matchmaking.application.ports import ChallengeRepository, RatingSnapshotProvider
from app.modules.matchmaking.application.services.pairing_service import _seat_rating
from app.modules.matchmaking.domain.challenge import (
    Challenge,
    ChallengeSelfNotAllowed,
    ChallengeStatus,
    challenge_match_id,
    issue,
)
from app.modules.matchmaking.domain.challenge_events import (
    FriendChallengeAccepted,
    FriendChallengeCancelled,
    FriendChallengeCreated,
    FriendChallengeDeclined,
)
from app.modules.rating.public import RatingSnapshot
from app.modules.reference.public import TimeControl, TimeControlCatalogue, TimeControlId
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)

#: How long a challenge match waits for both players to join.
#:
#: `domain-model.md` §10.3's `Created` join deadline. Ten minutes: long enough
#: that a challenger who stepped away between sending an invitation and its
#: acceptance still finds a game waiting, short enough that an abandoned one
#: does not hold a seat for an hour.
CHALLENGE_MATCH_JOIN_WINDOW: Final = timedelta(minutes=10)


class ChallengeNotFriends(RuleViolationError):
    """The two are not friends, **or** one has blocked the other.

    One error for both, deliberately — see the module docstring. A separate
    "blocked" code would tell somebody they had been blocked, which is the
    one thing BL-1 withholds.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.CHALLENGE_NOT_FRIENDS


class ChallengeInvalidTimeControl(RuleViolationError):
    """The requested clock is not one the platform currently offers.

    Raised for an identifier that has been retired as well as one that never
    existed: a control removed from the catalogue must not be choosable for a
    new game, while rows that already reference it stay readable.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.CHALLENGE_INVALID_TIME_CONTROL


class ChallengeService:
    """The four challenge use cases, over one unit of work."""

    def __init__(
        self,
        *,
        challenges: ChallengeRepository,
        social_graph: SocialGraphReader,
        exclusions: PairingExclusions,
        time_controls: TimeControlCatalogue,
        matches: MatchCreationUseCase,
        ratings: RatingSnapshotProvider,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._challenges = challenges
        self._events = events
        self._social_graph = social_graph
        self._exclusions = exclusions
        self._time_controls = time_controls
        self._matches = matches
        self._ratings = ratings
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
            # Staged inside the same transaction as the row, so the two
            # commit together or neither does.
            await self._events.publish(
                FriendChallengeCreated(
                    occurred_at=challenge.created_at,
                    challenge_id=challenge.id,
                    challenger_id=challenge.challenger_id,
                    recipient_id=challenge.recipient_id,
                    time_control_id=challenge.time_control_id,
                    variant=challenge.variant,
                    rated=challenge.rated,
                    expires_at=challenge.expires_at,
                )
            )
            await self._unit_of_work.commit()

        # Ids only. No player names, no settings — a log line that carried
        # who challenged whom by name would put the social graph in a log
        # aggregator.
        logger.info(
            "friend_challenge_created",
            extra={"challenge_id": str(challenge.id), "rated": challenge.rated},
        )
        return challenge

    async def accept(self, challenge_id: UUID, *, by: UUID) -> Challenge:
        """The recipient agrees, and the match that agreement produces.

        **One transaction, and the ordering inside it is the whole design.**

        `SessionUnitOfWork` is a scope marker over a session it does not own:
        entering is a no-op and `commit()` commits everything staged on that
        session. `MatchCreationUseCase` runs on the *same* session, so the
        challenge update staged before it commits with the match and with
        both sets of events — or, if anything raises, none of it does.

        That is why the challenge is saved **before** `create_match` rather
        than after: `create_match` commits, so a challenge written afterwards
        would land in a second transaction and a crash between them would
        leave a match with no accepted challenge. §10 forbids exactly that,
        and forbids emulating atomicity with compensating cleanup.

            1. load, scoped to a party
            2. re-check the relationship — it is mutable and the snapshot is
               not authority for it (§3)
            3. resolve the clock, which refuses a retired one (§7)
            4. `accept` — refuses a non-pending or expired challenge, and a
               challenger trying to accept their own
            5. save, guarded on `status = 'pending'`
            6. stage `FriendChallengeAccepted`
            7. create the match, which commits all of it

        ## How two services commit once

        `MatchCreationUseCase` commits by contract, which is correct for
        every caller that only creates a match and wrong for this one. Rather
        than special-casing that inside `game`, acceptance hands it a
        `ParticipatingUnitOfWork` — a unit of work that stages and flushes
        and leaves the commit to its caller. `game` is unchanged, its other
        callers are unchanged, and the caller that needs to own the
        transaction says so by construction.

        The match is created **first**, because its identity is generated at
        persistence and the challenge has to record it. The window that
        ordering would open — a match with no accepted challenge — does not
        exist, because nothing has committed until the last line.

        ## And why a second accept cannot make a second match

        `pairing_id` is derived from the challenge id, and `game` enforces
        `uq_match__pairing_id`. So even if the guarded challenge update let a
        second acceptance through, the second match could not be written
        (§20) — two independent defences for one invariant.
        """
        now = self._clock.now()
        challenge = await self.get(challenge_id, by=by)

        # Re-checked here rather than trusted from creation: a friendship can
        # end and a block can be placed in the twenty-four hours between the
        # two, and §3 is explicit that the earlier snapshot is not authority
        # for mutable relationship state.
        await self._require_friends(challenge.challenger_id, challenge.recipient_id)
        control = await self._time_controls.require(challenge.time_control_id)

        # Read **before** the write, and before the game exists: PR-3
        # requires the rating calculation to run on the values captured
        # before the match was played, which is what a seat snapshot is.
        # Keyed by the challenge's variant and the control's speed class,
        # exactly as the queue keys it — a seat stamped `classical` beside a
        # blitz rating would make `match_completed` unreconcilable.
        seats = {
            player_id: await self._ratings.rating_for(
                player_id, variant=challenge.variant, speed_class=control.speed_class
            )
            for player_id in (challenge.challenger_id, challenge.recipient_id)
        }

        async with self._unit_of_work:
            # **The match first**, because its identity is generated at
            # persistence and the challenge has to record it. `create_match`
            # holds a `ParticipatingUnitOfWork`, so it stages and flushes and
            # does **not** end the transaction — which is what lets the two
            # writes below join it.
            result = await self._matches.create_match(
                _match_request(challenge, control=control, seats=seats, at=now)
            )

            accepted = challenge.accept(by=by, at=now, match_id=result.match_id)
            await self._challenges.save(accepted)
            await self._events.publish(
                FriendChallengeAccepted(
                    occurred_at=now,
                    challenge_id=accepted.id,
                    challenger_id=accepted.challenger_id,
                    recipient_id=accepted.recipient_id,
                    match_id=result.match_id,
                    time_control_id=accepted.time_control_id,
                    variant=accepted.variant,
                    rated=accepted.rated,
                )
            )
            # One commit for the match, the challenge, `game.match_created`
            # and `matchmaking.friend_challenge_accepted`. A consumer sees
            # all four or none.
            await self._unit_of_work.commit()

        logger.info(
            "friend_challenge_accepted",
            extra={"challenge_id": str(accepted.id), "match_id": str(result.match_id)},
        )
        return accepted

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

    async def incoming(
        self, recipient_id: UUID, *, limit: int, cursor: str | None
    ) -> tuple[Sequence[Challenge], str | None]:
        """Live challenges this player has **received**, newest first.

        Read-only; opens no transaction. Returns the page and the next
        cursor — composing player profiles happens above this layer, which is
        what keeps this service free of any dependency on `profiles`.

        Live means pending, unexpired, **and still permitted** — see
        `_still_offerable`.
        """
        page, next_cursor = await self._challenges.list_for_party(
            recipient_id,
            as_challenger=False,
            now=self._clock.now(),
            limit=limit,
            cursor=cursor,
        )
        return await self._still_offerable(page, viewer_id=recipient_id), next_cursor

    async def outgoing(
        self, challenger_id: UUID, *, limit: int, cursor: str | None
    ) -> tuple[Sequence[Challenge], str | None]:
        """Live challenges this player has **sent**. See `incoming`."""
        page, next_cursor = await self._challenges.list_for_party(
            challenger_id,
            as_challenger=True,
            now=self._clock.now(),
            limit=limit,
            cursor=cursor,
        )
        return await self._still_offerable(page, viewer_id=challenger_id), next_cursor

    async def _still_offerable(
        self, page: Sequence[Challenge], *, viewer_id: UUID
    ) -> Sequence[Challenge]:
        """Drops challenges the current relationship no longer permits — §19, §20.

        A challenge outlives the friendship that allowed it: the row is the
        record that an invitation happened, and A64-022.1 does not delete
        history. What must not outlive it is the **invitation** — an
        actionable row offering a game with somebody who has since unfriended
        or blocked you.

        Removing the friendship is the general test and covers both cases,
        because a block also ends the friendship. That is why this module
        needs no notion of blocking: BL-2 is satisfied by asking whether they
        are friends, which is the question a challenge needed in the first
        place.

        **This is a visibility rule, not the security boundary.** The row is
        still stored and still readable by id, and acceptance re-checks the
        relationship in A64-022.3 — so a stale invitation cannot become a
        game even if something failed to hide it.

        ## Why it is applied here and not in the query

        The friendship lives in another module's schema. A join would be the
        cross-context reach `.importlinter` forbids and DM-06 designs
        against, so the page is fetched and then filtered — which makes
        `limit` an upper bound rather than an exact count. That is stated on
        the endpoints, and it is the honest trade: the alternative is
        `matchmaking` querying `friends`' tables.

        One batch read per page, never one per row.
        """
        if not page:
            return page

        others = {
            challenge.recipient_id
            if challenge.challenger_id == viewer_id
            else challenge.challenger_id
            for challenge in page
        }
        friends = await self._social_graph.friend_ids_among(viewer_id, list(others))
        return [
            challenge
            for challenge in page
            if (
                challenge.recipient_id
                if challenge.challenger_id == viewer_id
                else challenge.challenger_id
            )
            in friends
        ]

    async def get(self, challenge_id: UUID, *, by: UUID) -> Challenge:
        """One challenge, scoped to somebody who is part of it.

        Raises `NotFoundError` for a challenge that does not exist **and**
        for one between two other people — an id that answered differently
        would be an existence oracle (§25).
        """
        challenge = await self._challenges.get_for_party(challenge_id, party_id=by)
        if challenge is None:
            raise NotFoundError("No such challenge.")
        return challenge

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
            # **Only after a transition that actually happened.** `save`
            # raises on a row somebody else settled first, so this line is
            # unreachable for a losing writer — which is what stops a
            # duplicate decline emitting a second event.
            #
            # Expiry publishes nothing here: the sweep that writes the
            # terminal row owns that event (A64-022.6), and emitting from
            # both would announce one challenge twice.
            if settled.status is ChallengeStatus.DECLINED:
                await self._events.publish(
                    FriendChallengeDeclined(
                        occurred_at=now,
                        challenge_id=settled.id,
                        challenger_id=settled.challenger_id,
                        recipient_id=settled.recipient_id,
                    )
                )
            elif settled.status is ChallengeStatus.CANCELLED:
                await self._events.publish(
                    FriendChallengeCancelled(
                        occurred_at=now,
                        challenge_id=settled.id,
                        challenger_id=settled.challenger_id,
                        recipient_id=settled.recipient_id,
                    )
                )
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


def _match_request(
    challenge: Challenge,
    *,
    control: TimeControl,
    seats: Mapping[UUID, RatingSnapshot],
    at: datetime,
) -> CreateMatchRequest:
    """The challenge's stored settings, as `game`'s request — A64-022.3 §8.

    Every field comes from the challenge or from the platform. **Nothing
    comes from the accept request**, which carries no body: the recipient
    agrees to exactly the proposal already stored, and a settings field here
    would be a way to change what was agreed after agreeing to it (§5).

    ## Seats

    `Pairing.of`'s policy, reused rather than reinvented: the **parity of the
    pairing id** decides who plays light. Its docstring gives the reasoning —
    "whoever waited" and "lower rating" both hand a measurable edge to a
    predictable player, where a hash of two identifiers neither chose is
    stable across retries and even over many games.

    "The challenger always moves first" would have been exactly the kind of
    edge that argument rejects, so it is not what happens.

    ## Rating snapshots

    Read before the match exists and stored unchanged, because PR-3 requires
    the rating calculation to run on the values captured *before* the game
    was played. Through the same `RatingSnapshotProvider` the queue uses, so
    a challenge game and a queue game record a rating the same way.

    `queue_ticket_id` is `None` on both seats: nobody arrived through a
    queue. A64-019.5H made that column nullable for exactly this shape after
    A64-019.5 derived a fake ticket id to satisfy a `NOT NULL`.

    ## `BILATERAL`, and why not `SYSTEM`

    A challenge match is **timed**, and `MatchRecord` refuses a
    system-activated match that carries a time control: the first flag
    deadline is written when a match *activates*, and the one place that
    happens is `MatchAcceptanceService`. A `SYSTEM` match activates at
    creation, so a timed one would start a clock nothing had scheduled a
    deadline for — a game that can never flag. The invariant says so in as
    many words, and says it was written precisely so a later task would be
    made to schedule the deadline rather than discover months afterwards
    that nobody flags.

    Giving `game` that capability is `game`'s phase, not this one. So the
    match is created **pending**, and the existing queue handshake — which
    does schedule the deadline — activates it.

    That is also what `domain-model.md` §10.3 describes, read carefully: *"an
    accepted challenge whose challenger is offline still creates the match,
    which then resolves through the `Created` join deadline."* A match that
    resolves through a join deadline is one that was waiting to be joined.

    The consequence for a player is one tap, on a screen they are already
    looking at: the recipient accepts the challenge and then joins the game
    the same way they would join a queue match. A64-022.5's UI can make that
    a single action; the *protocol* is the one that already works.
    """
    pairing_id = challenge_match_id(challenge.id)
    light_first = pairing_id.int % 2 == 0
    first, second = sorted((challenge.challenger_id, challenge.recipient_id), key=str)
    light, dark = (first, second) if light_first else (second, first)

    return CreateMatchRequest(
        # Derived from the challenge — `game`'s unique index on this is what
        # makes one challenge produce at most one match.
        pairing_id=pairing_id,
        # **R-25's round trip.** `origin` says a person invited another
        # person, and `origin_ref` is the challenge itself — so a match can
        # be traced back to the invitation that produced it, and `game`
        # stores both without interpreting either.
        #
        # Both are server-owned: nothing in the accept request reaches here,
        # so a client cannot claim a match came from a challenge it did not.
        origin=MatchOrigin.CHALLENGE,
        origin_ref=challenge.id,
        variant=challenge.variant,
        rated=challenge.rated,
        engine_version=game_engine_version(),
        acceptance=AcceptancePolicy.BILATERAL,
        acceptance_deadline=at + CHALLENGE_MATCH_JOIN_WINDOW,
        time_control=MatchTimeControl(
            initial_ms=control.base_time_ms,
            increment_ms=control.increment_ms,
        ),
        light=MatchParticipant(
            player_id=light,
            queue_ticket_id=None,
            rating=_seat_rating(seats[light], speed_class=control.speed_class),
        ),
        dark=MatchParticipant(
            player_id=dark,
            queue_ticket_id=None,
            rating=_seat_rating(seats[dark], speed_class=control.speed_class),
        ),
    )
