"""Random legal games, with the invariants asserted at every ply.

Perft explores the tree exhaustively and shallowly. The corpus explores it
by hand. This explores it deeply and arbitrarily: whole games played to
their natural end, checking after every move the things that must be true
of *any* game the engine will ever produce.

## Deterministic, despite the name

Every game is driven by an explicitly seeded `random.Random`. Nothing in
production has a random number generator — AD-13 forbids the kernel one —
so the randomness lives entirely in the test's choice of which legal move
to play, and the same seed replays the same game forever.

Every assertion carries its seed. A failure here is reproducible from the
message alone, which is the difference between a fuzz finding and a ghost.

## Why the seeds are a fixed list rather than a range

So that adding a seed is a deliberate act with a diff, and so that a
failing seed can be kept in the list after it is fixed — a fuzz finding
becomes a regression test by staying where it is.

## Cost

Random games finish naturally: 30–120 plies, every one of them reaching a
real terminal state. The expensive invariant is "replaying the log so far
reproduces the position so far", which is quadratic if asked at every ply
— so it is asked in two ways. Every game replays its **whole** log once,
and `ReplayEngine` verifies each ply's fingerprint internally as it goes,
which is the per-ply property at linear cost. A smaller set of games also
walks every prefix explicitly, which is the same property proved the
expensive way.
"""

import random

import pytest

from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    BoardVariant,
    IllegalMove,
    Move,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    PieceRank,
    PlayerSide,
    Position,
    TerminalStateEvaluator,
    initial_board,
)
from app.modules.game.domain import (
    DrawRuleSet,
    InvalidMatchTransition,
    Match,
    MatchOutcome,
    MatchStatus,
    ReplayData,
    ReplayEngine,
)

generator = MoveGenerator()
validator = MoveValidator(generator)
applier = MoveApplier(validator)
evaluator = TerminalStateEvaluator(generator)
draw_rules = DrawRuleSet()
replay_engine = ReplayEngine(applier, evaluator, draw_rules)

SEEDS = (0, 1, 2, 3, 5, 8, 13, 21, 34, 55)
"""Fixed, and a list rather than a range: adding one is a deliberate act
with a diff, and a seed that once failed stays here afterwards — which is
how a fuzz finding becomes a regression test."""

DEEP_SEEDS = (0, 1)
"""The seeds that also get the quadratic prefix walk."""

PLY_CAP = 400
"""A safety limit, not an expectation. Observed games end in 30–120 plies;
reaching this would mean the engine had found a way not to finish, which is
itself the finding."""

VARIANTS = tuple(BoardVariant)


def opening(variant: BoardVariant) -> Position:
    """The position `Match.create` starts a game from."""
    return Position(board=initial_board(variant), side_to_move=PlayerSide.LIGHT)


def _fail(seed: int, variant: BoardVariant, ply: int, what: str) -> str:
    return f"seed={seed} variant={variant.value} ply={ply}: {what}"


def _check_ply(match: Match, seed: int, variant: BoardVariant, before: Position) -> None:
    """Everything that must be true immediately after a successful move."""
    ply = match.ply_number
    log = match.move_log

    assert len(log) == ply, _fail(seed, variant, ply, "one record per successful move")
    assert [record.ply_number for record in log] == list(range(1, ply + 1)), _fail(
        seed, variant, ply, "ply numbers contiguous from 1 (MT-5)"
    )
    assert log[-1].resulting_position_hash == match.position.fingerprint, _fail(
        seed, variant, ply, "the recorded fingerprint is the position reached"
    )
    assert match.last_move == log[-1].move, _fail(seed, variant, ply, "one history, not two")

    assert before.board.piece_count() >= match.position.board.piece_count(), _fail(
        seed, variant, ply, "material never increases"
    )
    assert match.position.side_to_move is before.side_to_move.opponent(), _fail(
        seed, variant, ply, "the turn passes"
    )

    assert 0 <= match.plies_since_progress <= ply, _fail(
        seed, variant, ply, f"plies_since_progress={match.plies_since_progress} out of range"
    )
    assert 1 <= match.current_position_occurrences <= ply + 1, _fail(
        seed, variant, ply, "an occurrence count outside what the history can hold"
    )
    assert sum(match.position_history.values()) == ply + 1, _fail(
        seed, variant, ply, "every position reached is counted exactly once"
    )

    geometry = match.position.board.geometry
    assert all(geometry.is_playable(square) for square in match.position.board.occupied_squares), (
        _fail(seed, variant, ply, "a piece on a square the board does not have")
    )

    _check_result_is_coherent(match, seed, variant)


def _check_result_is_coherent(match: Match, seed: int, variant: BoardVariant) -> None:
    ply = match.ply_number
    result = match.result

    assert (result is not None) is match.status.is_final, _fail(
        seed, variant, ply, "a result exists exactly when the match has ended (DM-08)"
    )
    if result is None:
        return

    assert (result.winner is not None) is (result.outcome is MatchOutcome.WIN), _fail(
        seed, variant, ply, f"{result.outcome.value} result with winner={result.winner}"
    )
    if result.winner is not None:
        assert match.position.board.piece_count_for(result.winner) > 0, _fail(
            seed, variant, ply, "the winner has no pieces left"
        )


def play_random_game(seed: int, variant: BoardVariant, *, check_every_ply: bool = True) -> Match:
    rng = random.Random(seed)
    match = Match.create(variant, engine_version=CURRENT_ENGINE_VERSION)
    match.start()

    while match.status is MatchStatus.ACTIVE and match.ply_number < PLY_CAP:
        before = match.position
        legal = generator.legal_moves(before)
        if not legal:
            raise AssertionError(
                _fail(seed, variant, match.ply_number, "an active match with no legal moves")
            )

        chosen = rng.choice(legal)
        validator.validate(before, chosen)
        match.play(chosen, applier, evaluator, draw_rules)

        assert before == Position(board=before.board, side_to_move=before.side_to_move), _fail(
            seed, variant, match.ply_number, "the position played from was mutated"
        )
        if check_every_ply:
            _check_ply(match, seed, variant, before)

    return match


def recording(match: Match) -> ReplayData:
    return ReplayData(
        engine_version=match.engine_version,
        variant=match.variant,
        opening_position=opening(match.variant),
        records=match.move_log,
    )


class TestRandomGamesHoldTheirInvariants:
    @pytest.mark.parametrize("variant", VARIANTS, ids=[v.value for v in VARIANTS])
    @pytest.mark.parametrize("seed", SEEDS)
    def test_a_random_game_is_coherent_at_every_ply(self, seed: int, variant: BoardVariant) -> None:
        """The main sweep — thirty games, every invariant after every move.

        See `_check_ply` for the list: one record per move, contiguous ply
        numbers, the recorded fingerprint, material that only decreases,
        counters in range, a coherent result, and no piece on a square the
        board does not have.
        """
        match = play_random_game(seed, variant)

        assert match.ply_number < PLY_CAP, _fail(
            seed, variant, match.ply_number, "the game did not finish inside the safety cap"
        )
        assert match.status is MatchStatus.COMPLETED, _fail(
            seed, variant, match.ply_number, f"ended {match.status.value}"
        )

    @pytest.mark.parametrize("variant", VARIANTS, ids=[v.value for v in VARIANTS])
    @pytest.mark.parametrize("seed", SEEDS)
    def test_a_finished_game_accepts_no_further_move(
        self, seed: int, variant: BoardVariant
    ) -> None:
        match = play_random_game(seed, variant, check_every_ply=False)
        last = match.move_log[-1].move

        with pytest.raises((InvalidMatchTransition, IllegalMove)):
            match.play(last, applier, evaluator, draw_rules)

    @pytest.mark.parametrize("seed", SEEDS)
    def test_the_same_seed_plays_the_same_game(self, seed: int) -> None:
        """The property the whole file rests on. A fuzz suite that could not
        reproduce its own failures would be a random pass/fail generator."""
        one = play_random_game(seed, BoardVariant.RUSSIAN_8X8, check_every_ply=False)
        other = play_random_game(seed, BoardVariant.RUSSIAN_8X8, check_every_ply=False)

        assert one.move_log == other.move_log, f"seed={seed}"
        assert one.position == other.position, f"seed={seed}"
        assert one.result == other.result, f"seed={seed}"


class TestEveryRandomGameReplays:
    @pytest.mark.parametrize("variant", VARIANTS, ids=[v.value for v in VARIANTS])
    @pytest.mark.parametrize("seed", SEEDS)
    def test_the_whole_log_reproduces_the_game(self, seed: int, variant: BoardVariant) -> None:
        """`ReplayEngine` verifies each ply's fingerprint as it walks, so
        one full replay proves the per-ply property at linear cost —
        including the counters and occurrence counts, which are recomputed
        rather than restored."""
        played = play_random_game(seed, variant, check_every_ply=False)

        replayed = replay_engine.replay(recording(played))

        assert replayed.position == played.position, f"seed={seed} {variant.value}"
        assert replayed.result == played.result, f"seed={seed} {variant.value}"
        assert replayed.status is played.status, f"seed={seed} {variant.value}"
        assert replayed.ply_number == played.ply_number, f"seed={seed} {variant.value}"
        assert replayed.plies_since_progress == played.plies_since_progress
        assert replayed.current_position_occurrences == played.current_position_occurrences
        assert replayed.position_history == played.position_history
        assert replayed.move_log == played.move_log

    @pytest.mark.parametrize("variant", VARIANTS, ids=[v.value for v in VARIANTS])
    @pytest.mark.parametrize("seed", DEEP_SEEDS)
    def test_every_prefix_of_the_log_replays_to_its_own_position(
        self, seed: int, variant: BoardVariant
    ) -> None:
        """The same property proved the expensive way, on a few games: for
        every *n*, `replay(log[:n])` reaches the position ply *n* recorded.

        Quadratic, so it is a handful of games rather than all of them.
        """
        played = play_random_game(seed, variant, check_every_ply=False)
        whole = recording(played)

        for plies, record in enumerate(played.move_log, start=1):
            prefix = ReplayData(
                engine_version=whole.engine_version,
                variant=whole.variant,
                opening_position=whole.opening_position,
                records=tuple(whole.records[:plies]),
            )

            replayed = replay_engine.replay(prefix)

            assert replayed.position.fingerprint == record.resulting_position_hash, _fail(
                seed, variant, plies, "a prefix replay reached a different position"
            )
            assert replayed.ply_number == plies


class TestProgressCountersAcrossWholeGames:
    @pytest.mark.parametrize("variant", VARIANTS, ids=[v.value for v in VARIANTS])
    @pytest.mark.parametrize("seed", DEEP_SEEDS)
    def test_the_counter_resets_exactly_when_progress_was_made(
        self, seed: int, variant: BoardVariant
    ) -> None:
        """Replayed independently of the aggregate's own bookkeeping: walk
        the log, decide from each move whether it was progress, and compare
        against what `Match` counted."""
        played = play_random_game(seed, variant, check_every_ply=False)
        rules = played.position.board.geometry.draw_rules

        position = opening(variant)
        expected = 0
        for record in played.move_log:
            mover = position.board.piece_at(record.move.origin)
            assert mover is not None
            progress = record.move.is_capture or (
                mover.rank is PieceRank.MAN and rules.man_moves_reset_progress
            )
            expected = 0 if progress else expected + 1
            position = applier.apply(position, record.move)

        assert played.plies_since_progress == expected, f"seed={seed} {variant.value}"


class TestMoveInvariantsAcrossWholeGames:
    @pytest.mark.parametrize("variant", VARIANTS, ids=[v.value for v in VARIANTS])
    @pytest.mark.parametrize("seed", DEEP_SEEDS)
    def test_every_generated_move_is_well_formed(self, seed: int, variant: BoardVariant) -> None:
        """Over a whole game rather than a hand-picked position: paths of
        at least two squares, no square captured twice, and a capture
        removing exactly as many pieces as it names."""
        played = play_random_game(seed, variant, check_every_ply=False)

        position = opening(variant)
        for record in played.move_log:
            move = record.move
            assert len(move.path) >= 2
            assert len(set(move.captured)) == len(move.captured)

            after = applier.apply(position, move)
            taken = position.board.piece_count() - after.board.piece_count()
            assert taken == len(move.captured), _fail(
                seed, variant, record.ply_number, "captured count and material loss disagree"
            )
            position = after

    @pytest.mark.parametrize("variant", VARIANTS, ids=[v.value for v in VARIANTS])
    @pytest.mark.parametrize("seed", DEEP_SEEDS)
    def test_a_capture_is_played_whenever_one_is_available(
        self, seed: int, variant: BoardVariant
    ) -> None:
        """Mandatory capture, checked over whole games: if any legal move in
        a position captures, then **every** legal move in it does."""
        played = play_random_game(seed, variant, check_every_ply=False)

        position = opening(variant)
        for record in played.move_log:
            legal: tuple[Move, ...] = generator.legal_moves(position)
            if any(move.is_capture for move in legal):
                assert all(move.is_capture for move in legal), _fail(
                    seed, variant, record.ply_number, "a quiet move offered beside a capture"
                )
            position = applier.apply(position, record.move)
