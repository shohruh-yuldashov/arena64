"""`DrawRuleSet` — has this game drawn?

Framework-free (architecture.md §8). Pure and side-effect free: the same
rules and the same history give the same answer, always.

## Why draws are here and not in `engine`

domain-model.md MT-12: "terminal detection consults game **history**, not
just the position." `TerminalStateEvaluator` is the half that reads a
position and can therefore only report a *loss* — a side with no pieces or
no moves. Every draw in draughts is the other half: the same position
occurring often enough, or too many plies without progress. A board cannot
show either.

So `TerminalStateEvaluator`'s contract is unchanged by this task, and
deliberately: widening it would mean giving the kernel a memory, which is
the one thing AD-13 does not allow it, and it would make "is this position
terminal" a question with a different answer depending on how the game got
there.

## Why it takes a snapshot rather than the `Match`

`evaluate(rules, history)` — two immutable values, neither of which is the
aggregate. Three reasons:

1. **It can be tested against configurations no variant has.** Only the
   repetition threshold is documented (see `DrawRules`), so the other
   three rules would be unreachable code if this took a `Match`, whose
   rules come from `geometry_of(variant)`. Taking the config makes every
   branch exercisable without inventing a variant.
2. **It cannot change the match.** A collaborator handed the aggregate can
   mutate it; one handed a frozen snapshot cannot, and "side-effect free"
   stops being a promise and becomes a property.
3. **The dependency points the right way.** `Match` knows about the rule
   set; the rule set knows nothing about `Match`.

## The order rules are checked in

Repetition, then king-only, then material bands, then the plain
no-progress limit — most specific first. All four can be true at once in a
thin endgame, and the reason a player reads should be the one that
actually describes their game.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.modules.engine import DrawRules, MaterialPlyLimit, PlayerSide


class DrawReason(StrEnum):
    """Which rule drew the game.

    Finer than `TerminationReason`, which records `move_limit` for the
    last three of these. That enum is domain-model.md §15's closed
    enumeration and R-19 fixes its members; this one is the rules engine's
    own vocabulary, and `Match` maps it on the way out. The same split as
    `engine.TerminalReason` against `TerminationReason`, for the same
    reason: what a rule knows is finer than what a permanent record keeps.

    **Whether that conflation is acceptable is an open question**, flagged
    in `specs/game-engine.md` §7.7: three different rules become one
    recorded reason, and a statistic that wanted to tell "shuffled to a
    draw" from "repeated to a draw" cannot. Widening §15 is a documented
    -model change and was not made unilaterally here.
    """

    REPETITION = "repetition"
    """One position occurred as often as the variant allows."""

    NO_PROGRESS = "no_progress"
    """Too many plies without a capture or a man's move."""

    KING_ONLY_MOVE_LIMIT = "king_only_move_limit"
    """The same, under the shorter limit that applies once no men remain."""

    MATERIAL_MOVE_LIMIT = "material_move_limit"
    """The same, under a limit that thin material shortens."""


@dataclass(frozen=True, slots=True)
class MatchHistory:
    """Everything a draw rule needs, and nothing else.

    A frozen snapshot rather than the aggregate — see the module
    docstring. Every field is a number `Match` already maintains or can
    count from the position it already holds; nothing here is stored twice.
    """

    current_position_occurrences: int
    """How often the position now on the board has occurred this game,
    counting the position itself. See `DrawRuleSet` on why the opening is
    occurrence 1."""

    plies_since_progress: int
    """Plies since the last capture or man's move."""

    total_pieces: int
    light_kings: int
    dark_kings: int

    @property
    def men_remaining(self) -> int:
        return self.total_pieces - self.light_kings - self.dark_kings

    @property
    def kings_only(self) -> bool:
        """Whether every piece left on the board is a king."""
        return self.men_remaining == 0

    def kings_for(self, side: PlayerSide) -> int:
        return self.light_kings if side is PlayerSide.LIGHT else self.dark_kings


class DrawRuleSet:
    """Applies a variant's draw thresholds to a match's history."""

    def evaluate(self, rules: DrawRules, history: MatchHistory) -> DrawReason | None:
        """Which rule has drawn the game, or `None` if none has.

        `None` is the overwhelmingly common answer and is modelled in the
        return type rather than as a "not drawn" member, for the reason
        DM-08 gives about `MatchResult`: a sentinel invites the code that
        computes ratings to forget to check it.
        """
        if _has_repeated(rules, history):
            return DrawReason.REPETITION
        if _exceeded(rules.king_only_ply_limit if history.kings_only else None, history):
            return DrawReason.KING_ONLY_MOVE_LIMIT
        if _exceeded(_material_limit(rules, history), history):
            return DrawReason.MATERIAL_MOVE_LIMIT
        if _exceeded(rules.no_progress_ply_limit, history):
            return DrawReason.NO_PROGRESS
        return None


def _has_repeated(rules: DrawRules, history: MatchHistory) -> bool:
    """Whether the current position has occurred often enough.

    **The first occurrence counts.** A game's opening position has
    occurred once before anybody has moved, so a threshold of three fires
    on the *second* return to a position — which is what "three-fold" has
    always meant, and why `Match` records the opening at creation rather
    than after the first move.
    """
    if rules.repetition_threshold is None:
        return False
    return history.current_position_occurrences >= rules.repetition_threshold


def _material_limit(rules: DrawRules, history: MatchHistory) -> int | None:
    """The ply limit thin material imposes, or `None` if no band matches.

    The first matching band wins, so bands are configured narrowest first.
    Returning `None` rather than infinity keeps "no band applies" and "a
    very patient band applies" distinguishable.
    """
    for band in rules.material_ply_limits:
        if _band_matches(band, history):
            return band.ply_limit
    return None


def _band_matches(band: MaterialPlyLimit, history: MatchHistory) -> bool:
    """Whether the board is thin enough for `band`.

    Conditions are ANDed, and an unset one is no condition at all —
    `MaterialPlyLimit` refuses a band with neither, which would match
    everything and make every band after it unreachable.
    """
    if band.max_pieces is not None and history.total_pieces > band.max_pieces:
        return False
    return band.max_kings_per_side is None or (
        history.light_kings <= band.max_kings_per_side
        and history.dark_kings <= band.max_kings_per_side
    )


def _exceeded(limit: int | None, history: MatchHistory) -> bool:
    """Whether the no-progress count has reached `limit`.

    `>=`, so a limit of *n* plies draws on the *n*th ply without progress
    rather than the one after it. `None` — the rule is not configured —
    is never exceeded.
    """
    return limit is not None and history.plies_since_progress >= limit


__all__ = ["DrawReason", "DrawRuleSet", "MatchHistory"]
