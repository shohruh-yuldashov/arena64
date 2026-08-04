"""The game clock — A64-016.5 §1.

Two frozen values and a handful of pure functions. Nothing here reads a
clock: every operation takes the instant it is about explicitly, which is
AD-07's rule and is what makes a flag race testable without sleeping.

## Why a time control is optional

`reference.time_control` does not exist (database.md §6.2 specifies it;
nothing implements it), so `QueuePool` cannot carry one and `matchmaking`
cannot supply one. Every match this platform has created is therefore
**untimed**, and an untimed match must keep working exactly as it does
today: no deadline, no flag, and null clock columns on its moves.

So `TimeControl | None` is the honest model rather than a placeholder
control with an enormous budget. A `None` control means the clock machinery
does not run at all — which is checkable, where "the budget is a week" is a
number somebody eventually treats as real.

## Why the clock version is the ply

§5 asks a deadline to carry a version and says to reuse the existing
compare-and-set pattern. The ply already is that version: it advances
exactly when the position changes, it is already under the match row's lock,
and it is already what `uq_move__ply` serialises on.

A second sequence would be a second thing to keep in step with the first,
and the failure it would produce — a deadline that is current by one counter
and stale by the other — is precisely the ambiguity a version exists to
remove.

## What is deliberately absent

No delay (Bronstein or simple), no multi-stage controls, no Fischer-vs-delay
distinction beyond the increment. `TimeControl` has the two fields the
platform can actually populate today, and adding a mode nothing produces
would be the speculative generality CLAUDE.md §1.7 forbids — the same
argument `TokenType.ACCESS` and A64-016.1's four-member `MessageType` both
record.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.modules.engine import PlayerSide

#: The smallest unit the clock reasons in.
#:
#: Milliseconds throughout, and never seconds: a bullet increment is two
#: seconds and a flag margin is tens of milliseconds, so a second-resolution
#: clock would round a player's remaining time to zero while they still had
#: some. `database.md` §8.4 already specifies the move log's clock columns in
#: milliseconds, so this is that decision honoured rather than remade.
MILLISECONDS_PER_SECOND = 1000


@dataclass(frozen=True, slots=True)
class TimeControl:
    """How much time each side gets, and what a move earns back.

    Frozen and validated at construction: a control is configuration, and a
    zero or negative budget is not a fast game — it is a match every player
    loses on their first move.
    """

    initial_ms: int
    """Each side's budget at the start. Both sides get the same, which is
    the only arrangement the platform offers; an odds game would be a
    product decision rather than a field."""

    increment_ms: int = 0
    """What a side gets back after each of its own moves — Fischer.

    Applied **after** the elapsed time is charged, which is the ordering
    that matters: charging after crediting would let a player who moved
    instantly bank the increment against time they had not yet spent.
    """

    def __post_init__(self) -> None:
        if self.initial_ms <= 0:
            raise ValueError("a time control gives each side a positive budget")
        if self.increment_ms < 0:
            raise ValueError("an increment cannot be negative")


@dataclass(frozen=True, slots=True)
class ClockState:
    """Both clocks, and which of them is running.

    A value rather than a pair of mutable counters, so "the state after this
    move" is a new object the caller can compare against the one it read —
    which is what makes the compare-and-set at the storage boundary a
    comparison of facts rather than of side effects.
    """

    light_ms: int
    dark_ms: int
    """What each side has left. Non-negative: a flagged clock reads zero
    rather than negative, because "how far past the flag" is a diagnostic
    the `received_at` comparison already answers precisely and a negative
    balance would be a second, worse answer to it."""

    active_side: PlayerSide
    """Whose clock is running. Equal to the position's `side_to_move` by
    construction — the clock switches in the same operation the move does —
    and carried anyway, because a deadline claimed by a worker has to be
    checkable against the side it was written for without loading a
    position."""

    turn_started_at: datetime
    """When the running clock last started.

    The instant elapsed time is measured **from**. For the first move it is
    when the match became active; afterwards it is the `received_at` of the
    previous move, so the platform's own processing time between two moves
    is charged to nobody.
    """

    def __post_init__(self) -> None:
        if self.light_ms < 0 or self.dark_ms < 0:
            raise ValueError("a clock does not hold negative time")

    @classmethod
    def start(cls, control: TimeControl, *, at: datetime) -> "ClockState":
        """Both clocks at their initial budget, LIGHT to move.

        `at` is when the match became playable. Nothing here reads a clock —
        see this module's docstring.
        """
        return cls(
            light_ms=control.initial_ms,
            dark_ms=control.initial_ms,
            active_side=PlayerSide.LIGHT,
            turn_started_at=at,
        )

    def remaining(self, side: PlayerSide) -> int:
        """What `side` has left, ignoring time running now."""
        return self.light_ms if side is PlayerSide.LIGHT else self.dark_ms

    def elapsed_ms(self, at: datetime) -> int:
        """How long the active side has been thinking, as of `at`.

        Never negative. A clock instant before the turn started is a clock
        skew between two processes, not a player who moved before their turn
        began, and charging a negative amount would credit them for it.
        """
        return max(0, int((at - self.turn_started_at).total_seconds() * MILLISECONDS_PER_SECOND))

    def deadline(self) -> datetime:
        """When the active side flags if they do not move.

        The instant a worker adjudicates against and the one a client counts
        down to. Derived rather than stored, so it cannot disagree with the
        remaining time it is computed from.
        """
        return self.turn_started_at + timedelta(milliseconds=self.remaining(self.active_side))

    def has_flagged(self, at: datetime) -> bool:
        """Whether the active side's time ran out at or before `at`.

        **Strictly after** the deadline, so an instant exactly on it is not
        a flag. That boundary is A64-016.5 §7's, and the reason it falls
        this way is that a move received *at* its deadline arrived in time —
        the player used all of their budget and none of anybody else's, and
        losing there would make the platform's rounding the arbiter.
        """
        return at > self.deadline()

    def charged(self, control: TimeControl, *, at: datetime) -> "ClockState":
        """The clock after the active side moves at `at`.

        Charge, then credit, then switch — and the order is the whole of it:

        1. the elapsed time comes off the mover's budget, floored at zero so
           a flagged clock reads zero rather than negative
        2. the increment goes on **after**, so a player who moved instantly
           cannot bank it against time they had not spent
        3. the other side becomes active, and their turn starts at `at`

        `turn_started_at` becomes the mover's `received_at` rather than the
        instant this ran, which is MT-9: the platform's own processing delay
        between receiving a frame and committing it is charged to nobody.
        """
        spent = self.elapsed_ms(at)
        remaining = max(0, self.remaining(self.active_side) - spent) + control.increment_ms

        if self.active_side is PlayerSide.LIGHT:
            return replace(
                self,
                light_ms=remaining,
                active_side=PlayerSide.DARK,
                turn_started_at=at,
            )
        return replace(
            self,
            dark_ms=remaining,
            active_side=PlayerSide.LIGHT,
            turn_started_at=at,
        )


__all__ = ["MILLISECONDS_PER_SECOND", "ClockState", "TimeControl"]
