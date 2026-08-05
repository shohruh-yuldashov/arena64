"""Draw agreement — the negotiation two players hold beside the board.
A64-020.5C-pre §2, §3.

## Why this is `MatchRecord` state and not `Match` state

`Match` is rebuilt from the durable move log on every submission
(`LiveMoveService._rebuild`), which is what makes the log authoritative
rather than decorative. A draw offer is **not derivable from the log** — no
move records it — so a field on `Match` would be silently reset to nothing
by the next replay.

It belongs where the other facts about *the contest rather than the game*
live: `MatchRecord`, which `match_record.py` describes as "the platform"
half of the aggregate, whose transitions are "a consequence of a person"
rather than of the rules. Offering, accepting and declining are exactly
that.

## The re-offer rule is ply arithmetic, not a timer

§3 forbids a wall clock, a Redis TTL and an in-process timer, and the
reason each is wrong is the same: none of them survives a restart, and a
player who reconnects must not find their spam allowance refreshed.

What survives is the ply, because it is already durable, already
monotonic and already under the match row's lock. So eligibility is stored
as *the earliest ply at which this side may offer again* and compared
against `ply_number`. Nothing to expire, nothing to sweep, and a process
that dies mid-game reloads the identical answer.

**The parity is structural.** `Match.play` increments the ply before
appending, and LIGHT moves first, so LIGHT's moves land on odd plies and
DARK's on even ones. That is what lets "the opponent has completed one more
move" be computed rather than searched for — see `_next_ply_of`.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from app.modules.engine import PlayerSide

#: The ply from which a side with no standing restriction may offer.
#:
#: Zero rather than `None`, so the comparison is total: `ply_number` is
#: never negative, so `ply_number >= UNRESTRICTED` is always true and the
#: "no restriction" case needs no branch — here, in the repository, or in
#: the CHECK constraint.
UNRESTRICTED: int = 0


@dataclass(frozen=True, slots=True)
class DrawOffer:
    """A standing offer of a draw, from one side to the other."""

    offered_by: PlayerSide
    offered_at_ply: int
    """The match's ply when the offer was made.

    Kept for display and audit rather than for the rule: the re-offer
    threshold is computed at *resolution* time, because that is when the
    ply that matters is known.
    """

    offered_at: datetime

    @property
    def recipient(self) -> PlayerSide:
        """Who may answer. Always the opponent — an offer to oneself is not
        a state this type can express."""
        return self.offered_by.opponent()

    def is_from(self, side: PlayerSide) -> bool:
        return self.offered_by is side

    def is_to(self, side: PlayerSide) -> bool:
        return self.recipient is side


@dataclass(frozen=True, slots=True)
class DrawAgreement:
    """A match's whole draw-agreement state.

    Frozen, like `MatchRecord` itself and for the same reason: every write
    is a compare-and-set into storage, which needs the before and the after
    as two values.
    """

    offer: DrawOffer | None = None

    light_may_offer_from_ply: int = UNRESTRICTED
    dark_may_offer_from_ply: int = UNRESTRICTED
    """The earliest ply at which each side may make a *new* offer.

    Two fields rather than one and a side, because both players can be
    under a restriction at once: LIGHT offers and DARK declines, then DARK
    offers and LIGHT declines, and LIGHT's restriction has not necessarily
    lapsed.
    """

    def __post_init__(self) -> None:
        if self.light_may_offer_from_ply < UNRESTRICTED:
            raise ValueError("a re-offer threshold cannot be negative")
        if self.dark_may_offer_from_ply < UNRESTRICTED:
            raise ValueError("a re-offer threshold cannot be negative")
        if self.offer is not None and self.offer.offered_at_ply < 0:
            raise ValueError("an offer cannot be made before the game starts")

    @property
    def is_pending(self) -> bool:
        return self.offer is not None

    def threshold_for(self, side: PlayerSide) -> int:
        return (
            self.light_may_offer_from_ply
            if side is PlayerSide.LIGHT
            else self.dark_may_offer_from_ply
        )

    def may_offer(self, side: PlayerSide, *, at_ply: int) -> bool:
        """Whether `side` may open a new offer at this ply — §3.

        Two conditions, and both are about *this* side: nothing may be
        pending at all, and this side's own restriction must have lapsed.
        The opponent's restriction is irrelevant — a player is not blocked
        by having declined.
        """
        return not self.is_pending and at_ply >= self.threshold_for(side)

    def opened(self, offer: DrawOffer) -> "DrawAgreement":
        """This state with `offer` standing. Thresholds are untouched —
        making an offer does not restrict anybody; resolving one does."""
        return replace(self, offer=offer)

    def resolved(self, *, at_ply: int) -> "DrawAgreement":
        """This state with the pending offer cleared and its offerer put
        under the re-offer restriction — §3.

        One method for **every** way an offer ends short of acceptance,
        because the restriction is identical for all of them and three
        copies of this arithmetic is three places to get the parity wrong.

        Returns `self` unchanged when nothing was pending, so a caller that
        resolves twice — a duplicate frame, a move on a match with no offer
        — is a no-op rather than a spurious restriction on a player who
        never offered.
        """
        if self.offer is None:
            return self

        threshold = _next_ply_of(self.offer.recipient, after=at_ply)
        if self.offer.is_from(PlayerSide.LIGHT):
            return DrawAgreement(
                offer=None,
                light_may_offer_from_ply=threshold,
                dark_may_offer_from_ply=self.dark_may_offer_from_ply,
            )
        return DrawAgreement(
            offer=None,
            light_may_offer_from_ply=self.light_may_offer_from_ply,
            dark_may_offer_from_ply=threshold,
        )

    def settled(self) -> "DrawAgreement":
        """This state with nothing pending, for a match that has ended.

        The thresholds are dropped too: a completed match cannot be offered
        a draw, so keeping them would be bookkeeping about a game nobody can
        play. It also makes the CHECK constraint on `game.match` simple —
        a terminal row carries no agreement state at all.
        """
        return DrawAgreement()


def _next_ply_of(side: PlayerSide, *, after: int) -> int:
    """The earliest ply strictly after `after` on which `side` could move.

    LIGHT moves on odd plies and DARK on even ones, because `Match.play`
    increments before appending and LIGHT moves first. Ply `0` is the
    position before anybody has moved and belongs to neither.

    This is the whole of §3's "one additional authoritative move", and it
    is worth walking both cases it serves:

        declined at ply P      the recipient has not moved. Their next move
                               is P+1 if the parity fits, else P+2 — so the
                               offerer waits for exactly one move

        cleared by a move      the recipient moved *at* P, so P has their
                               parity and the next is P+2 — one move beyond
                               the one that resolved it, which is what §3
                               asks for

    One formula, both cases, because "the earliest ply the recipient could
    next move on" is the same question in each.
    """
    candidate = after + 1
    return candidate if _side_at_ply(candidate) is side else candidate + 1


def _side_at_ply(ply: int) -> PlayerSide:
    """Whose move a ply is. LIGHT plays the odd ones."""
    return PlayerSide.LIGHT if ply % 2 == 1 else PlayerSide.DARK


__all__ = ["UNRESTRICTED", "DrawAgreement", "DrawOffer"]
