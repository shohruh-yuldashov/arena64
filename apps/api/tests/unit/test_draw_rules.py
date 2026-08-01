"""`DrawRuleSet` — A64-014.7.

Most of this file exercises rules **no variant configures**. Only the
repetition threshold is documented in this repository (see
`engine/draw_rules.py`), so the move-limit rules would be unreachable code
if they could only be reached through a `Match`, whose rules come from
`geometry_of(variant)`.

That is the reason `evaluate` takes `(rules, history)` rather than a
`Match`: every branch is exercisable against a configuration written here,
without inventing a variant to carry numbers nobody has decided. When the
thresholds are settled they become one table edit, and these tests already
cover what they will do.
"""

import inspect

import pytest

from app.modules.engine import (
    THREEFOLD_REPETITION_ONLY,
    BoardCoordinate,
    BoardVariant,
    DrawRules,
    MaterialPlyLimit,
    Move,
    PieceRank,
    PlayerSide,
    geometry_of,
)
from app.modules.game.domain import DrawReason, DrawRuleSet, MatchHistory
from app.modules.game.domain import draws as draws_module
from app.modules.game.domain.match import is_progress

A1 = BoardCoordinate.parse("a1")
B2 = BoardCoordinate.parse("b2")
C3 = BoardCoordinate.parse("c3")
QUIET_KING_MOVE = Move(path=(A1, B2))
A_CAPTURE = Move(path=(A1, C3), captured=(B2,))

rule_set = DrawRuleSet()

NO_RULES = DrawRules()
"""Everything disabled — the shape a variant with no draw rules takes."""


def history(
    *,
    occurrences: int = 1,
    plies_since_progress: int = 0,
    total_pieces: int = 24,
    light_kings: int = 0,
    dark_kings: int = 0,
) -> MatchHistory:
    return MatchHistory(
        current_position_occurrences=occurrences,
        plies_since_progress=plies_since_progress,
        total_pieces=total_pieces,
        light_kings=light_kings,
        dark_kings=dark_kings,
    )


class TestRepetition:
    """The opening position is occurrence 1 before anybody has moved, so a
    threshold of three fires on the **second** return. `Match` records its
    starting position at creation for exactly this reason."""

    def test_the_opening_alone_is_not_a_draw(self) -> None:
        assert rule_set.evaluate(THREEFOLD_REPETITION_ONLY, history(occurrences=1)) is None

    def test_the_first_return_is_not_a_draw(self) -> None:
        assert rule_set.evaluate(THREEFOLD_REPETITION_ONLY, history(occurrences=2)) is None

    def test_the_second_return_draws(self) -> None:
        assert (
            rule_set.evaluate(THREEFOLD_REPETITION_ONLY, history(occurrences=3))
            is DrawReason.REPETITION
        )

    def test_a_count_beyond_the_threshold_still_draws(self) -> None:
        """`>=`, not `==`. A rule that only fired on equality would miss a
        game that somehow passed the threshold in one step."""
        assert (
            rule_set.evaluate(THREEFOLD_REPETITION_ONLY, history(occurrences=9))
            is DrawReason.REPETITION
        )

    def test_a_variant_without_the_rule_never_draws_by_repetition(self) -> None:
        assert rule_set.evaluate(NO_RULES, history(occurrences=99)) is None

    def test_the_threshold_is_configurable(self) -> None:
        fivefold = DrawRules(repetition_threshold=5)

        assert rule_set.evaluate(fivefold, history(occurrences=4)) is None
        assert rule_set.evaluate(fivefold, history(occurrences=5)) is DrawReason.REPETITION

    def test_a_threshold_below_two_is_refused(self) -> None:
        """One occurrence is the position itself, so a threshold of one
        would draw before the first move."""
        with pytest.raises(ValueError):
            DrawRules(repetition_threshold=1)


class TestNoProgressLimit:
    LIMIT = DrawRules(no_progress_ply_limit=40)

    def test_a_count_below_the_limit_is_not_a_draw(self) -> None:
        assert rule_set.evaluate(self.LIMIT, history(plies_since_progress=39)) is None

    def test_reaching_the_limit_draws(self) -> None:
        assert (
            rule_set.evaluate(self.LIMIT, history(plies_since_progress=40))
            is DrawReason.NO_PROGRESS
        )

    def test_an_unconfigured_limit_never_draws(self) -> None:
        assert rule_set.evaluate(NO_RULES, history(plies_since_progress=10_000)) is None

    def test_a_limit_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError):
            DrawRules(no_progress_ply_limit=0)


class TestKingOnlyLimit:
    RULES = DrawRules(no_progress_ply_limit=40, king_only_ply_limit=10)

    def test_it_applies_once_no_men_remain(self) -> None:
        kings = history(plies_since_progress=10, total_pieces=3, light_kings=2, dark_kings=1)

        assert rule_set.evaluate(self.RULES, kings) is DrawReason.KING_ONLY_MOVE_LIMIT

    def test_it_does_not_apply_while_a_man_is_on_the_board(self) -> None:
        """The shorter limit is for a board that cannot make progress by
        advancing. One man means it still can."""
        with_a_man = history(plies_since_progress=10, total_pieces=3, light_kings=2, dark_kings=0)

        assert rule_set.evaluate(self.RULES, with_a_man) is None

    def test_the_ordinary_limit_still_applies_to_a_king_only_board(self) -> None:
        patient = history(plies_since_progress=40, total_pieces=2, light_kings=1, dark_kings=1)

        assert rule_set.evaluate(DrawRules(no_progress_ply_limit=40), patient) is (
            DrawReason.NO_PROGRESS
        )


class TestMaterialLimits:
    RULES = DrawRules(
        no_progress_ply_limit=40,
        material_ply_limits=(
            MaterialPlyLimit(ply_limit=5, max_pieces=3),
            MaterialPlyLimit(ply_limit=15, max_pieces=5),
        ),
    )

    def test_a_thin_board_draws_sooner(self) -> None:
        thin = history(plies_since_progress=5, total_pieces=3, light_kings=1, dark_kings=1)

        assert rule_set.evaluate(self.RULES, thin) is DrawReason.MATERIAL_MOVE_LIMIT

    def test_the_first_matching_band_wins(self) -> None:
        """Bands are written narrowest first, so a three-piece board takes
        the five-ply limit rather than the fifteen-ply one."""
        five_pieces = history(plies_since_progress=5, total_pieces=5, light_kings=1, dark_kings=1)

        assert rule_set.evaluate(self.RULES, five_pieces) is None

    def test_a_board_no_band_matches_falls_through(self) -> None:
        crowded = history(plies_since_progress=15, total_pieces=20, light_kings=1, dark_kings=1)

        assert rule_set.evaluate(self.RULES, crowded) is None

    def test_a_band_can_key_on_kings_per_side(self) -> None:
        rules = DrawRules(
            material_ply_limits=(MaterialPlyLimit(ply_limit=3, max_kings_per_side=2),)
        )
        few_kings = history(plies_since_progress=3, total_pieces=9, light_kings=2, dark_kings=1)

        assert rule_set.evaluate(rules, few_kings) is DrawReason.MATERIAL_MOVE_LIMIT

    def test_a_side_over_the_king_band_does_not_match(self) -> None:
        rules = DrawRules(
            material_ply_limits=(MaterialPlyLimit(ply_limit=3, max_kings_per_side=2),)
        )
        many_kings = history(plies_since_progress=3, total_pieces=9, light_kings=3, dark_kings=1)

        assert rule_set.evaluate(rules, many_kings) is None

    def test_a_band_with_no_condition_is_refused(self) -> None:
        """It would match every position and make every band after it
        unreachable — invisible until a game ended early."""
        with pytest.raises(ValueError):
            MaterialPlyLimit(ply_limit=5)


class TestPrecedence:
    def test_repetition_is_reported_ahead_of_a_move_limit(self) -> None:
        """All four rules can be true at once in a thin endgame, and the
        reason a player reads should describe their game."""
        both = DrawRules(repetition_threshold=3, no_progress_ply_limit=4)

        verdict = rule_set.evaluate(both, history(occurrences=3, plies_since_progress=40))

        assert verdict is DrawReason.REPETITION

    def test_the_king_only_limit_is_reported_ahead_of_the_general_one(self) -> None:
        rules = DrawRules(no_progress_ply_limit=10, king_only_ply_limit=10)
        kings = history(plies_since_progress=10, total_pieces=2, light_kings=1, dark_kings=1)

        assert rule_set.evaluate(rules, kings) is DrawReason.KING_ONLY_MOVE_LIMIT


class TestDeterminism:
    def test_the_same_inputs_give_the_same_answer(self) -> None:
        snapshot = history(occurrences=3)

        assert rule_set.evaluate(THREEFOLD_REPETITION_ONLY, snapshot) == rule_set.evaluate(
            THREEFOLD_REPETITION_ONLY, snapshot
        )

    def test_evaluation_changes_nothing(self) -> None:
        """A frozen snapshot rather than the aggregate, so "side-effect
        free" is a property rather than a promise."""
        snapshot = history(occurrences=3, plies_since_progress=7)

        rule_set.evaluate(THREEFOLD_REPETITION_ONLY, snapshot)

        assert snapshot == history(occurrences=3, plies_since_progress=7)


class TestHistorySnapshot:
    def test_men_are_what_is_left_after_the_kings(self) -> None:
        assert history(total_pieces=10, light_kings=2, dark_kings=3).men_remaining == 5

    def test_a_board_of_kings_reports_kings_only(self) -> None:
        assert history(total_pieces=4, light_kings=2, dark_kings=2).kings_only

    def test_one_man_is_enough_to_not_be_kings_only(self) -> None:
        assert not history(total_pieces=4, light_kings=2, dark_kings=1).kings_only

    def test_kings_are_counted_per_side(self) -> None:
        snapshot = history(total_pieces=5, light_kings=3, dark_kings=1)

        assert (
            snapshot.kings_for(PlayerSide.LIGHT),
            snapshot.kings_for(PlayerSide.DARK),
        ) == (3, 1)


class TestProgressIsConfigured:
    """`captures_reset_progress` and `man_moves_reset_progress` are variant
    axes rather than constants (A64-014.7 §8). Both are true in every
    configured variant, so their `False` paths are exercised here."""

    def test_a_capture_is_progress_by_default(self) -> None:
        assert is_progress(A_CAPTURE, PieceRank.KING, NO_RULES)

    def test_a_variant_may_declare_that_captures_do_not_reset(self) -> None:
        assert not is_progress(A_CAPTURE, PieceRank.KING, DrawRules(captures_reset_progress=False))

    def test_a_man_move_is_progress_by_default(self) -> None:
        assert is_progress(QUIET_KING_MOVE, PieceRank.MAN, NO_RULES)

    def test_a_variant_may_declare_that_man_moves_do_not_reset(self) -> None:
        assert not is_progress(
            QUIET_KING_MOVE, PieceRank.MAN, DrawRules(man_moves_reset_progress=False)
        )

    def test_a_quiet_king_move_is_never_progress(self) -> None:
        assert not is_progress(QUIET_KING_MOVE, PieceRank.KING, NO_RULES)


class TestVariantConfiguration:
    def test_every_variant_declares_its_draw_rules(self) -> None:
        for variant in BoardVariant:
            assert geometry_of(variant).draw_rules is not None

    def test_every_variant_enables_threefold_repetition(self) -> None:
        """The one threshold this repository documents — domain-model.md
        states the "three-fold repetition draw rule" unqualified by
        variant."""
        for variant in BoardVariant:
            assert geometry_of(variant).draw_rules.repetition_threshold == 3

    def test_no_variant_configures_a_move_limit(self) -> None:
        """A recorded gap, not an oversight: no document in this repository
        gives a value for `moveless_draw_plies`, the king-only limit, or the
        material bands. Guessing one would end rated games on a number
        nobody chose."""
        for variant in BoardVariant:
            rules = geometry_of(variant).draw_rules
            assert rules.no_progress_ply_limit is None
            assert rules.king_only_ply_limit is None
            assert not rules.material_ply_limits

    def test_the_rule_set_names_no_variant(self) -> None:
        """The rule that keeps a second variant a table edit rather than a
        search for hidden branches. Asserted against the source, because it
        is the one property a behavioural test cannot show: a branch on a
        variant nobody has configured yet would pass every other test here.
        """
        source = inspect.getsource(draws_module)

        for variant in BoardVariant:
            assert variant.name not in source
            assert variant.value not in source
