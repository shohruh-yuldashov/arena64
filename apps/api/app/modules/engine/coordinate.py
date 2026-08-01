"""`BoardCoordinate` — the address of one square, on no board in particular.

Framework-free and dependency-free (AD-13). No clock, no randomness, no
configuration: a coordinate is a pair of integers and the rules that make a
pair meaningless.

## Row and column, not a PDN number

domain-model.md §2.1 defines a square as "one of the 32 playable dark
squares on an 8x8 board (50 on 10x10), **numbered per PDN**", and notation
is where this platform will eventually have to meet the outside world.
This type is still `(row, column)` rather than that number, because PDN
numbering is a *variant's* projection of the geometry — the same physical
square is 1 on an 8x8 board and 1 on a 10x10 board while being a different
square — and a value object that cannot be interpreted without knowing the
variant is a value object that will be misinterpreted.

The number is a rendering of a coordinate under a variant, and it belongs
with the variant, in the task that needs notation. Nothing here forecloses
it.

## Orientation

`row` increases away from LIGHT: row 0 is LIGHT's back rank and the last
row is DARK's. `column` increases left to right from LIGHT's point of
view. Both are zero-based, so the near-left corner is `(0, 0)`.

Fixing this here rather than in the board is what lets the initial
position, and later the direction a man may move, be stated once instead of
per variant.

## Why validation stops at the largest supported board

`__post_init__` refuses anything outside `0 .. MAX_BOARD_DIMENSION - 1`,
which is a weaker rule than "on the board" — `(9, 9)` is a legal
`BoardCoordinate` and is off the edge of an 8x8 board.

That split is deliberate: a coordinate does not know which board it is
being used against, and one that carried a variant would either duplicate
the board's geometry or make every square incomparable across variants.
The board owns "on *this* board"; this type owns "addressable at all", and
both refusals are the same `InvalidCoordinate` so a caller never has to
know which layer objected.

The bound exists at all so that `__str__` is total and so that a negative
index can never quietly wrap into a valid square — Python's `-1` indexing
is precisely the bug this class is here to make impossible.
"""

from dataclasses import dataclass
from string import ascii_lowercase

from app.modules.engine.exceptions import InvalidCoordinate

MAX_BOARD_DIMENSION = 10
"""The side length of the largest board the platform addresses.

Ten, because `BoardVariant.INTERNATIONAL_10X10` is the widest variant
configured. `BoardGeometry` asserts that every variant fits inside this,
so the two cannot drift: adding a 12x12 variant is a deliberate change
here, not a silent widening there.
"""

_COLUMN_LETTERS = ascii_lowercase[:MAX_BOARD_DIMENSION]
"""Column letters for `__str__`, one per addressable column.

Derived from the bound rather than written out, so the two cannot drift:
a wider board widens this automatically, and `__str__` can never index
past the end. CLAUDE.md §8.10 — logging never throws.
"""


@dataclass(frozen=True, slots=True, order=True)
class BoardCoordinate:
    """One square's address.

    **Frozen**, so it is hashable and usable as a dictionary key — which is
    exactly how `Board` stores a position, and how a future repetition
    check will compare two of them. domain-model.md §16.1 rejects an entity
    `Board` on the grounds that "the three-fold repetition draw rule
    requires positions to compare by value"; that guarantee starts here.

    **Ordered**, row-major, so any iteration over squares has one
    deterministic sequence. Positions get hashed and logged, and a hash
    that depended on dictionary insertion order would differ between two
    engines that agree about the position.
    """

    row: int
    """Zero-based rank, increasing away from LIGHT."""

    column: int
    """Zero-based file, increasing to LIGHT's right."""

    def __post_init__(self) -> None:
        if not 0 <= self.row < MAX_BOARD_DIMENSION:
            raise InvalidCoordinate(f"Row {self.row} is not an addressable rank.")
        if not 0 <= self.column < MAX_BOARD_DIMENSION:
            raise InvalidCoordinate(f"Column {self.column} is not an addressable file.")

    def __str__(self) -> str:
        """Algebraic notation — `a1` is the near-left corner.

        For logs, test failures and debugging only. It is not PDN and is
        not a wire format; see the module docstring.
        """
        return f"{_COLUMN_LETTERS[self.column]}{self.row + 1}"


__all__ = ["MAX_BOARD_DIMENSION", "BoardCoordinate"]
