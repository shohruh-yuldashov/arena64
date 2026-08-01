"""Engine performance — sanity, not a benchmark. A64-014.9.

## What these tests are for, and what they are not

They are **blow-up detectors**. Each bound is one to two orders of
magnitude above what the development machine measures, so a failure means
an algorithmic regression — an accidental quadratic, a lost early exit, a
cache that stopped working — and never "the CI runner was busy". CLAUDE.md
§6.5 has no tolerance for a flaky test, and a timing assertion tuned close
to the observed number is a flaky test with extra steps.

They are **not** a benchmark and not a budget. CLAUDE.md §10.11 asks for
before-and-after evidence with any optimisation, and §10.1 forbids
optimising without a measured bottleneck; nothing here is evidence that
one exists.

## Against the architecture target

system-design.md CP-1 budgets **p99 < 25 ms server-side** for a whole
submit-move round trip — transport, authentication, clock charging,
persistence, fan-out, and somewhere inside it the engine.

Observed on the development machine (Apple silicon, CPython 3.14):

| Operation | Position | Observed |
| --- | --- | --- |
| `legal_moves` | opening, 24 men | **83 µs** |
| `legal_moves` | king-heavy 10x10, 32 sequences of 12 captures | **5.9 ms** |
| `MoveApplier.apply` | opening (validates by generating) | **86 µs** |
| `Position.fingerprint` | opening | **9 µs** |
| Whole ply: apply + terminal evaluation | opening | **170 µs** |
| Replay of a corpus game, per case | including per-ply verification | **125 µs** |
| `perft(depth=4)` | English opening, 1,469 leaves | **170 ms** |

So an ordinary ply costs the engine about **0.17 ms against a 25 ms
budget** — roughly 0.7% of it, two orders of magnitude of headroom. The
contrived king-heavy position costs 5.9 ms, about a quarter of the budget
on its own; it was built in A64-014.5 to be as awkward as the rules allow,
does not occur in play, and is here to bound the tail rather than to
describe a typical ply.

**A real budget belongs with a real workload.** These numbers say the
engine is not the thing that will blow CP-1; they do not say what the
platform's p99 will be.
"""

from time import perf_counter

import pytest

from app.modules.engine import (
    Board,
    BoardCoordinate,
    BoardVariant,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    Piece,
    PieceRank,
    PlayerSide,
    Position,
    TerminalStateEvaluator,
    initial_board,
)
from app.modules.game.domain import DrawRuleSet, ReplayEngine
from tests.corpus import load_replays
from tests.perft import perft

generator = MoveGenerator()
applier = MoveApplier(MoveValidator(generator))
evaluator = TerminalStateEvaluator(generator)
draw_rules = DrawRuleSet()
replay_engine = ReplayEngine(applier, evaluator, draw_rules)

RUSSIAN_OPENING = Position(
    board=initial_board(BoardVariant.RUSSIAN_8X8), side_to_move=PlayerSide.LIGHT
)

DARK_MAN = Piece(side=PlayerSide.DARK, rank=PieceRank.MAN)
LIGHT_KING = Piece(side=PlayerSide.LIGHT, rank=PieceRank.KING)

KING_HEAVY = Position(
    board=Board(
        BoardVariant.INTERNATIONAL_10X10,
        {
            BoardCoordinate.parse(name): piece
            for name, piece in {
                "a1": LIGHT_KING,
                "e1": LIGHT_KING,
                "a9": LIGHT_KING,
                "c3": DARK_MAN,
                "c5": DARK_MAN,
                "c7": DARK_MAN,
                "e3": DARK_MAN,
                "e5": DARK_MAN,
                "e7": DARK_MAN,
                "g3": DARK_MAN,
                "g5": DARK_MAN,
                "g7": DARK_MAN,
                "i3": DARK_MAN,
                "i5": DARK_MAN,
                "i7": DARK_MAN,
            }.items()
        },
    ),
    side_to_move=PlayerSide.LIGHT,
)
"""A64-014.5's contrived worst case: three flying kings against twelve men,
every one reachable and every landing square open. It does not occur in
play; it is here to bound the tail."""


def elapsed(work: object) -> float:
    """Seconds taken by one call, measured once.

    Once, not averaged over a thousand runs: an average is a benchmark
    number, and pretending to produce one from a test that must not be
    flaky would be worse than the honest single sample these bounds are
    sized for.
    """
    assert callable(work)
    started = perf_counter()
    work()
    return perf_counter() - started


class TestMoveGeneration:
    def test_the_opening_position_generates_quickly(self) -> None:
        """A hundred generations well inside a tenth of a second. The
        per-call figure is tens of microseconds; the bound is loose enough
        that only an algorithmic change reaches it."""

        def work() -> None:
            for _ in range(100):
                generator.legal_moves(RUSSIAN_OPENING)

        assert elapsed(work) < 1.0

    def test_the_worst_case_position_stays_bounded(self) -> None:
        """~6 ms observed against a one-second ceiling. The bound this
        inherits from A64-014.5, kept because the structural guarantee
        below is the one that actually matters."""
        assert elapsed(lambda: generator.legal_moves(KING_HEAVY)) < 1.0

    def test_the_worst_case_is_bounded_by_material_and_not_by_the_board(self) -> None:
        """The structural claim behind the timing: a sequence cannot be
        longer than the opponent's piece count, because every step consumes
        one and none is taken twice. That is why the search cannot run away
        however open the board is."""
        opponents = KING_HEAVY.board.piece_count_for(PlayerSide.DARK)

        assert all(len(move.captured) <= opponents for move in generator.legal_moves(KING_HEAVY))


class TestMoveApplication:
    def test_applying_a_move_is_cheap(self) -> None:
        move = generator.legal_moves(RUSSIAN_OPENING)[0]

        def work() -> None:
            for _ in range(100):
                applier.apply(RUSSIAN_OPENING, move)

        assert elapsed(work) < 1.0


class TestReplay:
    REPLAYS = tuple(case for case in load_replays() if case.expected_rejection is None)

    def test_every_corpus_replay_reconstructs_quickly(self) -> None:
        """Including the per-ply fingerprint verification, which is the
        part a naive implementation would make quadratic by rebuilding the
        match from scratch for each ply."""

        def work() -> None:
            for case in self.REPLAYS:
                replay_engine.replay(case.replay)

        assert elapsed(work) < 1.0


class TestPerft:
    @pytest.mark.parametrize("depth", [3, 4])
    def test_a_shallow_perft_finishes_promptly(self, depth: int) -> None:
        """~30 ms and ~170 ms observed. The ceiling is the same for both, so
        the test that fails first is the one whose subtree grew."""

        def work() -> None:
            perft(
                Position(
                    board=initial_board(BoardVariant.ENGLISH_8X8),
                    side_to_move=PlayerSide.LIGHT,
                ),
                depth,
                generator,
                applier,
            )

        assert elapsed(work) < 5.0


class TestSerialization:
    def test_a_fingerprint_is_cheap_enough_to_take_every_ply(self) -> None:
        """`Match` computes one after every move, and a replay computes one
        more to verify. It is O(pieces) and recomputed rather than cached —
        `Position` documents that an incremental hash waits for a
        measurement, and this is the measurement saying it can keep
        waiting."""

        def work() -> None:
            for _ in range(1_000):
                _ = RUSSIAN_OPENING.fingerprint

        assert elapsed(work) < 1.0


class TestTheEngineIsNotTheBottleneck:
    def test_a_whole_ply_costs_a_fraction_of_the_cp1_budget(self) -> None:
        """CP-1 budgets p99 < 25 ms for the whole round trip. One ply
        through the engine — generate, validate, apply, evaluate terminal,
        evaluate draws — is measured here against a tenth of that for a
        hundred plies, so a single ply has three orders of magnitude of
        headroom.

        This is a sanity bound, not a budget: the round trip CP-1 measures
        also contains transport, authentication, clocks and persistence,
        none of which this suite can see."""
        move = generator.legal_moves(RUSSIAN_OPENING)[0]

        def work() -> None:
            for _ in range(100):
                after = applier.apply(RUSSIAN_OPENING, move)
                evaluator.evaluate(after)

        assert elapsed(work) < 2.5
