"""`DrawRules` — a variant's draw configuration.

Framework-free and dependency-free (AD-13), and **configuration only**:
nothing here evaluates anything. It states the thresholds; `game`'s
`DrawRuleSet` reads them against a match's history, because every draw in
draughts is a property of the game rather than of the board (MT-12) and
the engine sees only positions.

## Why the configuration is here and the evaluation is not

Because this is where every other variant axis lives, and because
database.md §6.1 puts these two on the variant reference table by name:
`reference.variant` has `repetition_draw_count` and `moveless_draw_plies`
beside `has_flying_kings` and `requires_maximum_capture`. A variant's draw
rules are part of the rule set a match was played under (MT-3), not part
of the match.

The evaluator cannot live here for the same reason
`TerminalStateEvaluator` cannot report a draw: it would need the position
history, and AD-13 gives the kernel no memory.

## Undecided thresholds — A64-014.7

**Only the repetition count is configured.** Everything else on this
record is `None`, which means the rule does not apply, and that is a
recorded gap rather than an implementation shortcut.

| Axis | Status |
| --- | --- |
| `repetition_threshold` | **3.** domain-model.md states the "three-fold |
| | repetition draw rule" throughout, unqualified by variant |
| `no_progress_ply_limit` | **Undecided.** database.md §6.1 names the |
| | column `moveless_draw_plies`; nothing anywhere gives it a value |
| `king_only_ply_limit` | **Undecided.** Russian draughts has a |
| | fifteen-move king-only rule; this repository does not say so |
| `material_ply_limits` | **Undecided.** Russian draughts scales the |
| | limit by material; the bands are nowhere stated |

Guessing a number here would be worse than leaving it off. A threshold
that ends games is a product rule, it becomes part of every rated result
the moment it ships, and AD-15 makes changing it an engine-version event.
The mechanism is complete and tested; the numbers are one table edit away
once somebody decides them.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class MaterialPlyLimit:
    """A move limit that applies only while the board holds little enough.

    Draughts variants scale the limit by what is left: a king-and-two
    against a king is adjudicated sooner than a middlegame. Each band is a
    row, and `DrawRuleSet` takes the first that matches — so bands are
    written narrowest first.

    Both conditions are optional and are ANDed when both are set. §12 of
    the task lists the inputs a real rule keys on — kings per side, men,
    total pieces — and these two cover the forms the documented rules
    would take; a band nobody has specified is not widened further on
    speculation.
    """

    ply_limit: int
    max_pieces: int | None = None
    """Applies while the board holds at most this many pieces in total."""

    max_kings_per_side: int | None = None
    """Applies while **neither** side has more than this many kings."""

    def __post_init__(self) -> None:
        if self.ply_limit < 1:
            raise ValueError("A move limit is a positive number of plies.")
        if self.max_pieces is None and self.max_kings_per_side is None:
            # A band with no condition would match every position and make
            # every band after it unreachable — the configuration mistake
            # that is invisible until a game ends early.
            raise ValueError("A material limit states at least one material condition.")


@dataclass(frozen=True, slots=True)
class DrawRules:
    """Every draw threshold a variant declares.

    `None` means the rule does not apply. That is a deliberate encoding
    rather than a sentinel to remember: a disabled rule and a rule with a
    threshold of zero are different things, and the second would draw
    every game immediately.
    """

    repetition_threshold: int | None = None
    """How many **occurrences** of one position end the game.

    Occurrences, not returns: the position a game starts from has occurred
    once. So a threshold of 3 fires on the *second* return to a position,
    which is what "three-fold" has always meant. `Match` records the
    opening at creation for exactly this reason.
    """

    no_progress_ply_limit: int | None = None
    """Plies without a capture or a man's move that end the game.

    Plies, not moves — one per player turn. database.md §6.1 names the
    column `moveless_draw_plies`, which settles the unit; the value is
    still undecided (see the module docstring).
    """

    king_only_ply_limit: int | None = None
    """Plies without progress that end the game once **no men remain**.

    A separate, usually shorter limit: a board of kings cannot make
    progress by advancing, so the ordinary no-progress limit is too
    patient for it.
    """

    material_ply_limits: Sequence[MaterialPlyLimit] = field(default_factory=tuple)
    """Bands that shorten the limit as material thins, narrowest first."""

    captures_reset_progress: bool = True
    """Whether a capture restarts the no-progress count. True everywhere:
    material only decreases, so a capture is irreversible."""

    man_moves_reset_progress: bool = True
    """Whether a man's move restarts it. True everywhere: a man only
    advances, so its move cannot be undone either."""

    @property
    def repetition_is_enabled(self) -> bool:
        return self.repetition_threshold is not None

    def __post_init__(self) -> None:
        if self.repetition_threshold is not None and self.repetition_threshold < 2:
            # One occurrence is the position itself. A threshold of 1 would
            # draw before the first move.
            raise ValueError("A repetition threshold counts at least two occurrences.")
        for limit in (self.no_progress_ply_limit, self.king_only_ply_limit):
            if limit is not None and limit < 1:
                raise ValueError("A ply limit is positive, or absent.")


THREEFOLD_REPETITION_ONLY = DrawRules(repetition_threshold=3)
"""The only draw configuration this repository has the documentation to
state — see the module docstring on the three that are undecided."""


__all__ = ["THREEFOLD_REPETITION_ONLY", "DrawRules", "MaterialPlyLimit"]
