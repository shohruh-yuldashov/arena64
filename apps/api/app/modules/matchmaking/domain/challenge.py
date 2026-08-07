"""A direct invitation from one player to a friend — A64-022.1.

Framework-free, and owned by `matchmaking` rather than by a module of its
own. That is `domain-model.md`'s placement, not a preference: §10.3 lists
`Challenge` beside `QueueTicket` in this context, and the reason is visible
in its own comparison table — both are *intentions to play* that resolve
into a `Match`, and they differ in who chooses the opponent and how long the
intention lives, not in what they are.

    | | QueueTicket | Challenge |
    | Opponent  | chosen by rating   | named at creation |
    | Lifetime  | seconds to minutes | hours to days     |
    | Resolution| the pairing worker | the recipient     |

## What this phase builds, and what it deliberately does not

A64-022.1 is the aggregate and its persistence. There is **no HTTP surface,
no realtime frame, no notification and no `Match`** — those are A64-022.2
and later.

The consequence worth stating plainly is `ACCEPTED`. It is a member of the
status enum and **nothing in this phase can reach it**. `domain-model.md`
§10.3 is explicit that "acceptance creates the match in the same transaction
that consumes the challenge", so an `accept()` that moved the status and
created no match would be a lie in the type system — a challenge that says
it was accepted with no game to show for it. The transition arrives with
match creation, in one place, in A64-022.3.

`decline`, `cancel` and `expire` have no such coupling: each is a terminal
state and nothing downstream has to exist for it to be true.

## Frozen, like every aggregate here

A transition returns a **new** instance. The repository writes what it is
given, so nothing can mutate a challenge in memory and forget to save it,
and a caller holding the old value still holds the old value.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Final
from uuid import UUID

from app.core.error_codes import ErrorCode
from app.core.exceptions import PermissionDeniedError, RuleViolationError
from app.modules.game.public import ProductVariant
from app.modules.reference.public import TimeControlId

#: How long a challenge stays answerable — A64-022.1.
#:
#: `domain-model.md` §10.3 gives the shape ("hours to days, survives
#: sign-out") and no number; twenty-four hours is the product decision taken
#: for this phase.
#:
#: The reasoning is a person in another time zone. A challenge sent on a
#: Monday evening is seen on a Tuesday evening at the latest, whatever hours
#: the two players keep — and a day is short enough that an unanswered
#: invitation stops being a live commitment before either has forgotten
#: sending it. It is also the window `auth`'s verification link uses, so the
#: platform has one "a day" rather than two.
CHALLENGE_TTL: Final = timedelta(hours=24)


class ChallengeStatus(StrEnum):
    """Where a challenge stands. Only `PENDING` may transition.

    `domain-model.md` §10.3's lifecycle names are `Offered → (Accepted →
    Consumed | Declined | Withdrawn | Expired | Voided-by-block)`. Three
    deliberate differences:

        `PENDING` for `Offered`     the word the rest of this platform uses
                                    for "sent and unanswered" — a friend
                                    request is `pending`, a delivery is
                                    `pending`. One vocabulary
        `CANCELLED` for `Withdrawn` likewise: `friends` already cancels a
                                    request
        no `Consumed`               `ACCEPTED` *is* consumption, because
                                    acceptance creates the match in the same
                                    transaction. A separate `Consumed` would
                                    be a state between two halves of one
                                    commit, which cannot be observed

    `Voided-by-block` is **absent and deferred**, not dropped. A block placed
    after a challenge is sent should void it, and doing that needs a consumer
    of `friends.player_blocked` — which is a phase with an event subscriber
    in it, not this one. Adding an unreachable member for it would leave two
    states nothing can produce; see `ACCEPTED` on why one is already the
    most this phase should carry.
    """

    PENDING = "pending"
    """Sent, unanswered, and not yet past `expires_at`."""

    ACCEPTED = "accepted"
    """The recipient agreed and a match exists.

    **Unreachable in A64-022.1.** See the module docstring: the transition
    belongs with match creation, and a status that could be set without one
    would describe a game nobody can play.
    """

    DECLINED = "declined"
    """The recipient said no. Terminal, and carries no reason — a declined
    invitation that explained itself would be a message, and this is not a
    messaging feature."""

    CANCELLED = "cancelled"
    """The challenger withdrew it. Terminal."""

    EXPIRED = "expired"
    """Past `expires_at` without an answer. Terminal.

    Reached by a sweep rather than by a read: see `is_expired_at` on why the
    row still has to be written."""


#: The states from which nothing may happen.
#:
#: A frozenset rather than `status is not PENDING`, so the terminal set is a
#: thing a reader can see and a future state has to be classified rather than
#: defaulting into "transitionable".
TERMINAL_STATUSES: Final[frozenset[ChallengeStatus]] = frozenset(
    {
        ChallengeStatus.ACCEPTED,
        ChallengeStatus.DECLINED,
        ChallengeStatus.CANCELLED,
        ChallengeStatus.EXPIRED,
    }
)


@dataclass(frozen=True, slots=True)
class Challenge:
    """One player's invitation to one friend, and the settings it fixes.

    ## What it stores, and what it refuses to

    The **settings a match needs**, so that A64-022.3 can create one without
    asking the client again: the time control, the variant and whether the
    challenger asked for it to be rated. Nothing else.

    No usernames, no display names, no avatars, no rating snapshots. Those
    are `profiles`' and `rating`'s to answer and they change; a challenge
    that copied them would be showing yesterday's name on today's screen, and
    would be a second place a private profile could leak from.

    No human-readable time control label either. `TimeControlId` is the
    authority and a label is a rendering of it — storing "3+2" would make the
    challenge's opinion of the clock able to disagree with `reference`'s.
    """

    id: UUID

    challenger_id: UUID
    """Who sent it. An opaque cross-context identifier (DM-06) — no foreign
    key, and nothing here can resolve it to a person."""

    recipient_id: UUID
    """Who was invited. Same rules."""

    time_control_id: TimeControlId
    """Which clock, by stable code.

    A `TimeControlId` rather than a UUID, because that is what `reference`
    publishes: the code *is* the identity and it is chosen to survive a
    change of label. Validated against the **active** catalogue at creation
    (`ChallengeService`), so a control retired since remains readable on old
    rows and cannot be chosen for a new one.
    """

    variant: ProductVariant
    """Which rule set.

    Stored although the platform offers exactly one today. `CreateMatchRequest`
    requires it, so A64-022.3 would otherwise have to invent a value at
    acceptance — and a challenge that did not record which game it was for
    would become ambiguous the day a second variant ships, retroactively, for
    every row already written.
    """

    rated: bool
    """Whether the **challenger asked** for this to count.

    A request, not an agreement — and the distinction is the product rule.
    Arena64 requires both players to agree before a direct game affects
    ratings, because two friends who could rate a game between themselves
    can move rating between themselves.

    So this field is one half. The other half is the recipient's explicit
    consent at acceptance, which lives in A64-022.3 along with acceptance
    itself. Nothing in this phase can produce a rated match, because nothing
    in this phase can produce a match.
    """

    status: ChallengeStatus

    created_at: datetime
    expires_at: datetime

    responded_at: datetime | None = None
    """When it left `PENDING`, or `None` while it has not.

    One column for all four terminal transitions rather than four, because
    `status` already says which happened and a `declined_at` beside a
    `cancelled_at` would be three nulls on every row.
    """

    created_match_id: UUID | None = None
    """The match acceptance produced, or `None`.

    **Always `None` in A64-022.1** — the column exists so that A64-022.3's
    migration is not a schema change on a live table, and so that the
    aggregate's shape does not change when acceptance arrives.

    It is written by the platform in the same transaction that creates the
    match. A client cannot supply it, which is what stops a forged link
    between a challenge and somebody else's game.
    """

    def is_expired_at(self, moment: datetime) -> bool:
        """Whether this is past its window at `moment`.

        A **read-time** answer, and it does not replace the stored state.
        `PENDING` past `expires_at` is not acceptable and this is what
        refuses it — but the row must still be swept to `EXPIRED`, because
        an event nobody emitted is a notification nobody sends, and A64-022.6
        is where the sweep lives.

        Both are needed. Without the read-time check, a challenge could be
        accepted in the window between expiry and the sweep; without the
        sweep, `EXPIRED` would be a state the database never holds and every
        reader would have to re-derive it.
        """
        return moment >= self.expires_at

    @property
    def is_pending(self) -> bool:
        return self.status is ChallengeStatus.PENDING

    def decline(self, *, by: UUID, at: datetime) -> "Challenge":
        """The recipient says no.

        Only the recipient. A challenger who could decline their own
        challenge would be cancelling it under another name, and the two are
        different facts to anybody reading the history.
        """
        self._require_answerable(at)
        if by != self.recipient_id:
            raise ChallengeForbidden("only the recipient may decline a challenge")
        return self._settle(ChallengeStatus.DECLINED, at)

    def cancel(self, *, by: UUID, at: datetime) -> "Challenge":
        """The challenger withdraws it.

        Permitted **past expiry**, unlike accepting and declining, and that
        asymmetry is deliberate: cancelling an expired challenge is somebody
        tidying up a list, and refusing it would leave a row they cannot
        clear until a sweep they cannot see runs.
        """
        if not self.is_pending:
            raise ChallengeNotPending("this challenge has already been answered")
        if by != self.challenger_id:
            raise ChallengeForbidden("only the challenger may cancel a challenge")
        return self._settle(ChallengeStatus.CANCELLED, at)

    def expire(self, *, at: datetime) -> "Challenge":
        """The window closed with no answer.

        No actor: this is the platform's own transition, which is why it
        takes no `by`. Refuses to run early, so a sweep with a wrong clock
        cannot cancel live challenges under a different name.
        """
        if not self.is_pending:
            raise ChallengeNotPending("this challenge has already been answered")
        if not self.is_expired_at(at):
            raise RuleViolationError("this challenge has not expired yet")
        return self._settle(ChallengeStatus.EXPIRED, at)

    def _require_answerable(self, at: datetime) -> None:
        """Pending, and still inside its window.

        The order matters for the message: a challenge that was declined an
        hour ago and has since passed its expiry should say it was answered,
        not that it expired.
        """
        if not self.is_pending:
            raise ChallengeNotPending("this challenge has already been answered")
        if self.is_expired_at(at):
            raise ChallengeExpired("this challenge has expired")

    def _settle(self, status: ChallengeStatus, at: datetime) -> "Challenge":
        """The one place a terminal state is written.

        `responded_at` is set here rather than by each caller, so the
        invariant "a terminal challenge has a response time" holds by
        construction — and the timestamp is the caller's injected clock
        (AD-07), never `datetime.now()`.
        """
        return replace(self, status=status, responded_at=at)


def issue(
    *,
    challenge_id: UUID,
    challenger_id: UUID,
    recipient_id: UUID,
    time_control_id: TimeControlId,
    variant: ProductVariant,
    rated: bool,
    at: datetime,
) -> Challenge:
    """A new pending challenge, expiring `CHALLENGE_TTL` from `at`.

    A function rather than a classmethod, matching this module's other
    aggregates, and the only place `expires_at` is derived — so the window is
    one arithmetic expression rather than one per call site.

    The **self-challenge** check is here rather than in the service, because
    it is the one rule that needs nothing but the two ids: a challenge to
    oneself is not a policy decision that could be configured differently, it
    is a value that cannot exist. Friendship, blocking and the time control
    catalogue all require a reader and belong to the service.
    """
    if challenger_id == recipient_id:
        raise ChallengeSelfNotAllowed("a player cannot challenge themselves")

    return Challenge(
        id=challenge_id,
        challenger_id=challenger_id,
        recipient_id=recipient_id,
        time_control_id=time_control_id,
        variant=variant,
        rated=rated,
        status=ChallengeStatus.PENDING,
        created_at=at,
        expires_at=at + CHALLENGE_TTL,
    )


class ChallengeSelfNotAllowed(RuleViolationError):
    """A player named themselves as the recipient."""

    default_code: ClassVar[ErrorCode] = ErrorCode.CHALLENGE_SELF_NOT_ALLOWED


class ChallengeNotPending(RuleViolationError):
    """The challenge has already reached a terminal state.

    One error for all four, deliberately. "Already declined" and "already
    cancelled" are the same answer to the caller — there is nothing to do —
    and distinguishing them would tell a challenger whether their invitation
    was declined or had merely expired, which is a small disclosure nobody
    asked for.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.CHALLENGE_NOT_PENDING


class ChallengeExpired(RuleViolationError):
    """Still `PENDING` in the database, and past its window.

    Distinct from `ChallengeNotPending` because the remedy differs: an
    expired challenge can be sent again, where an answered one has been
    answered.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.CHALLENGE_EXPIRED


class ChallengeForbidden(PermissionDeniedError):
    """The actor is a party to this challenge and not the right one.

    A **permission** error rather than a rule violation, so it answers `403`
    rather than `422` — A64-022.2 §3. The distinction is real: `422` says the
    request was understood and the state refuses it, where this says the
    caller is not the person who may do this. A challenger who has been
    told `422` will retry.

    Never raised for a stranger: a challenge somebody is not part of is
    reported as **not found**, so an identifier cannot be probed for
    existence (§25's IDOR rule). This is the challenger trying to decline, or
    the recipient trying to cancel — both of whom already know it exists.
    """


__all__ = [
    "CHALLENGE_TTL",
    "TERMINAL_STATUSES",
    "Challenge",
    "ChallengeExpired",
    "ChallengeForbidden",
    "ChallengeNotPending",
    "ChallengeSelfNotAllowed",
    "ChallengeStatus",
    "issue",
]
