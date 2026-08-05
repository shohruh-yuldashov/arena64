"""The time control catalogue — database.md §6.2, A64-020.5A-pre §2.

Framework-free (architecture.md §8): no SQL, no clock, no HTTP. Three types,
and the split between them is the whole design of this module.

## Why the identifier is an enum and the definition is a row

`TimeControlId` is a closed `StrEnum` in code; `base_time_ms`,
`increment_ms`, `speed_class`, the label and the ordering live in
`reference.time_control`. That looks like two sources of truth and is not:
they answer different questions.

    which controls exist          the enum
    what each one actually is     the row

The split is what makes the rest of the platform work. `QueuePool` gains a
fourth component and stays **pure** — it can validate an identifier and
round-trip it through a task payload without a database — and `every_pool()`
stays a function of enums rather than a startup query. A plain string would
give up both, and would put an unvalidated value in a pool identifier that a
pairing scan is dispatched with.

It is also the shape this platform already uses for exactly this kind of
data. `ProductVariant` and `Region` are closed enums whose membership is a
code change, and `Region`'s docstring says why: a closed list is worth the
deploy, and a native PostgreSQL enum on the column is what stops a typo
becoming a value no read path knows how to evaluate.

What the **row** buys, and the enum could not: `is_active` retires a control
without a code change, `display_order` and `label` are presentation that
should not be a deploy, and the values themselves are constrained by
`CHECK`s rather than by a constructor nobody runs on a backfill.

An enum member with no active row is not a contradiction — it is a control
that has been withdrawn, and `TimeControlCatalogue.require` answers it with
`UnknownTimeControl`. The reverse cannot happen: the column is the same
native enum.

## Why a snapshot type exists beside the catalogue entry

A queue ticket and a match are **permanent records of a choice**, and a
catalogue is metadata somebody may edit. `TimeControlSnapshot` is the subset
that must never be re-read: the four facts that decide what game is played
and which rating it moves.

A ticket that stored only the identifier would be reinterpreted the moment
an operator corrected a row — a player who queued for 3+2 would be paired
into whatever 3+2 means now. `SeatRating` makes the identical argument for
the identical reason, and PR-3 is the rule both of them serve.

## What is deliberately absent

**No delay.** database.md §6.2 gives `reference.time_control` a `delay_ms`
and `game.domain.clock.TimeControl` has no such field — Bronstein and simple
delay are a clock behaviour nothing on this platform implements. A column
that is always zero would be a contract claiming the clock honours a delay
it would silently ignore.

**No correspondence control.** SPEC-RATING's `SpeedClass` has the member and
this catalogue does not use it: a correspondence game is one whose clock runs
for days, which needs a deadline model the flag worker does not have
(`GAME_CLOCK_INTERVAL_SECONDS` is one second and its index is fleet-wide).
When correspondence ships it is a row and a member, not a redesign.

**No player-authored controls.** A client submits an identifier and never a
duration — see `TimeControlCatalogue`.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.modules.rating.public import SpeedClass


class TimeControlId(StrEnum):
    """Which time controls the platform offers, by stable code.

    The value is the wire format, the pool identifier's fourth component and
    the database enum's member, so it is chosen to survive a change of
    label: `blitz_3_2` says what the control *is* rather than what a menu
    currently calls it.

    Ordered here as they are offered — fastest first — which is the order
    `display_order` seeds and the order a client renders. The enum's own
    order is not authoritative (`display_order` is, and it is a column
    precisely so it can change without a deploy); it is kept in step so the
    two do not read as disagreeing.
    """

    BULLET_1_0 = "bullet_1_0"
    BLITZ_3_2 = "blitz_3_2"
    RAPID_10_0 = "rapid_10_0"
    CLASSICAL_30_0 = "classical_30_0"


@dataclass(frozen=True, slots=True)
class TimeControlSnapshot:
    """What a permanent record copies when a control is chosen.

    Frozen and validated, and carried by `QueueTicket` and by
    `CreateMatchRequest` — see this module's docstring on why a stored
    identifier alone would be a record that changes meaning.

    Four fields, and each has exactly one reader:

        id             which pool this ticket belongs to
        base_time_ms   the clock `game` starts
        increment_ms   what a move earns back
        speed_class    which rating the result moves
    """

    id: TimeControlId
    base_time_ms: int
    increment_ms: int
    speed_class: SpeedClass

    def __post_init__(self) -> None:
        # Re-checked here rather than only at the catalogue's `CHECK`s,
        # because a repository rehydrating a ticket constructs this
        # directly — the same argument `QueueTicket.__post_init__` makes.
        if self.base_time_ms <= 0:
            raise ValueError("a time control gives each side a positive budget")
        if self.increment_ms < 0:
            raise ValueError("an increment cannot be negative")


@dataclass(frozen=True, slots=True)
class TimeControl:
    """One row of the catalogue: a control, and how it is presented.

    The snapshot plus the three fields that are *metadata* — a label a
    client renders, an order it renders them in, and whether it may still be
    chosen. Nothing durable copies these, which is exactly why they are
    separated from `snapshot`.
    """

    snapshot: TimeControlSnapshot
    label: str
    """What a menu calls it — "1+0", "3+2". Free text, and the one field an
    operator may change without any record anywhere becoming wrong."""

    display_order: int
    """Where it sits in the list. A column rather than the enum's order, so
    reordering the menu is not a deploy."""

    is_active: bool
    """Whether it may still be chosen.

    Retirement rather than deletion: a control that stopped being offered is
    still the control a thousand finished matches were played under, and
    deleting the row would orphan every one of them. `false` removes it from
    the menu and refuses new tickets; it changes nothing already recorded.
    """

    @property
    def id(self) -> TimeControlId:
        return self.snapshot.id

    @property
    def base_time_ms(self) -> int:
        return self.snapshot.base_time_ms

    @property
    def increment_ms(self) -> int:
        return self.snapshot.increment_ms

    @property
    def speed_class(self) -> SpeedClass:
        return self.snapshot.speed_class


__all__ = ["TimeControl", "TimeControlId", "TimeControlSnapshot"]
