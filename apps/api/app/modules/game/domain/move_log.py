"""`MoveRecord` — one ply, as the permanent record keeps it.

Framework-free (architecture.md §8).

domain-model.md MT-6 states the shape: "a move is recorded with its full
capture path, the resulting position hash, the mover's think time, and the
remaining clock after the move." MT-5 states the discipline: "the move log
is append-only, ordered, and gap-free; ply numbers are contiguous from 1 —
a gap makes the game unreplayable, which invalidates the result, the
analysis, and the fair-play record simultaneously."

## The clock fields are absent, not invented

`think_time_ms` and `remaining_clock_ms` are `None` on every record this
build writes, because the engine has no clock and A64-014.8 is forbidden
one. They are declared anyway, for MT-6's reason: "think time: `fairplay`
cannot be retrofitted (AD-05); remaining clock: the only way to reconstruct
a disputed flag." A log written now without the fields would be a log that
has to be migrated when clocks arrive, and the games recorded in between
would have a hole nothing can fill.

`None` says "not measured". A zero would say "measured, and it was
instant", which is a different and false claim.

## `resulting_position_hash` is a fingerprint, not a Zobrist hash

It is `Position.fingerprint` — a sorted, deterministic string of the
variant, the side to move and every occupied square. Stable across
processes, machines and languages, which is what a replay and a
cross-implementation corpus need.

It is **not** a Zobrist hash and must not be described as one. It is not
incremental, it is not fixed-width, and it has no XOR structure to update
in place. domain-model.md §10.1 reserves `PositionHash` for that, and
architecture.md §11 lists "position hashing for repetition rules" among
what the engine will eventually do; when it arrives it will be a second,
narrower thing beside this one, adopted with a measurement behind it.
"""

from dataclasses import dataclass

from app.modules.engine import Move


@dataclass(frozen=True, slots=True)
class MoveRecord:
    """One entry in a match's move log.

    Frozen. MT-5 makes the log append-only, and a mutable record would
    make "append-only" a convention rather than a property — the entry
    could be edited in place while the sequence looked untouched.
    """

    ply_number: int
    """Contiguous from 1 — MT-5. `Match` assigns it; nothing else should."""

    move: Move
    """The complete move, path and all. Never an origin and a destination:
    see `engine.serialization.move_to_primitive`."""

    resulting_position_hash: str
    """`Position.fingerprint` of the position this move produced.

    Recorded so a replay can check itself ply by ply rather than only at
    the end — a divergence caught on the move that caused it names the
    rule that changed, and one caught at the end names nothing.
    """

    think_time_ms: int | None = None
    """How long the mover took. `None` until clocks exist — see the module
    docstring on why the field is here anyway."""

    remaining_clock_ms: int | None = None
    """What the mover had left afterwards. `None` until clocks exist."""

    def __post_init__(self) -> None:
        if self.ply_number < 1:
            raise ValueError("Ply numbers are contiguous from 1 (MT-5).")
        for measurement in (self.think_time_ms, self.remaining_clock_ms):
            if measurement is not None and measurement < 0:
                raise ValueError("A clock measurement is not negative.")


__all__ = ["MoveRecord"]
