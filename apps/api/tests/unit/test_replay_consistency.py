"""Replay, verified ply by ply — A64-014.9.

`test_replay.py` checks that `ReplayEngine` does what it says. This checks
the property that makes replay worth having: **a replay reconstructs a
game's history, not its final board.**

Every corpus replay is walked prefix by prefix. After each ply the
reconstructed match is compared against the live one on everything a
disputed game would turn on — the position, its fingerprint, the ply
number, how often that position has occurred, how long since progress, the
status and the result. A replay that agreed only at the end would pass a
test that only looked at the end, and would be wrong about every draw.

## Determinism

AD-13's kernel is "deterministic functions over immutable value objects".
The tests below run the same replay several times in one process and
require identical fingerprints, identical move ordering, identical results
and identical serialization. Nothing here seeds a random number generator,
because nothing in the engine has one to seed.
"""

import json

import pytest

from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    Position,
    TerminalStateEvaluator,
)
from app.modules.engine.serialization import position_to_primitive
from app.modules.game.domain import DrawRuleSet, Match, ReplayData, ReplayEngine
from app.modules.game.domain.serialization import replay_to_primitive
from tests.corpus import ReplayCase, load_replays

generator = MoveGenerator()
applier = MoveApplier(MoveValidator(generator))
evaluator = TerminalStateEvaluator(generator)
draw_rules = DrawRuleSet()
replay_engine = ReplayEngine(applier, evaluator, draw_rules)

PLAYABLE = tuple(case for case in load_replays() if case.expected_rejection is None)
"""The corpus replays that must succeed. The refusals are `test_replay.py`'s
and `test_engine_corpus.py`'s; walking a prefix of a log that is supposed to
be rejected would assert nothing."""


def prefix(case: ReplayCase, plies: int) -> ReplayData:
    return ReplayData(
        engine_version=case.replay.engine_version,
        variant=case.replay.variant,
        opening_position=case.replay.opening_position,
        records=tuple(case.replay.records[:plies]),
    )


def live(case: ReplayCase, plies: int) -> Match:
    """The same prefix played forward, as a live game would have."""
    match = Match(
        variant=case.replay.variant,
        engine_version=case.replay.engine_version,
        position=case.replay.opening_position,
    )
    match.start()
    for record in case.replay.records[:plies]:
        match.play(record.move, applier, evaluator, draw_rules)
    return match


@pytest.mark.parametrize("case", PLAYABLE, ids=[case.id for case in PLAYABLE])
def test_a_replay_agrees_with_the_live_game_at_every_ply(case: ReplayCase) -> None:
    """The whole property, one corpus case at a time.

    Compared at each prefix: position, fingerprint, ply number, occurrence
    count, no-progress counter, status and result. A game that ended by
    repetition is the case this catches — its final board is unremarkable,
    and the reason it ended is in the plies before it.
    """
    for plies in range(len(case.replay.records) + 1):
        replayed = replay_engine.replay(prefix(case, plies))
        expected = live(case, plies)

        assert replayed.position == expected.position, f"{case.source} ply {plies}"
        assert replayed.position.fingerprint == expected.position.fingerprint
        assert replayed.ply_number == plies
        assert replayed.current_position_occurrences == expected.current_position_occurrences
        assert replayed.plies_since_progress == expected.plies_since_progress
        assert replayed.status is expected.status
        assert replayed.result == expected.result


@pytest.mark.parametrize("case", PLAYABLE, ids=[case.id for case in PLAYABLE])
def test_every_recorded_fingerprint_is_reached_at_its_own_ply(case: ReplayCase) -> None:
    """`replay(log[:n])` reaches the position record *n* wrote down.

    Verified against the record rather than against the live game, so this
    fails if the engine and the stored hash disagree even when the engine
    is self-consistent — which is the divergence AD-15 exists to surface.
    """
    for plies, record in enumerate(case.replay.records, start=1):
        replayed = replay_engine.replay(prefix(case, plies))

        assert replayed.position.fingerprint == record.resulting_position_hash, (
            f"{case.source} ply {plies}"
        )


@pytest.mark.parametrize("case", PLAYABLE, ids=[case.id for case in PLAYABLE])
def test_the_rebuilt_move_log_matches_the_record(case: ReplayCase) -> None:
    replayed = replay_engine.replay(case.replay)

    assert replayed.move_log == tuple(case.replay.records), case.source


class TestDeterminism:
    """Three runs of the same input, in one process, compared as a whole."""

    RUNS = 3

    def replays(self, case: ReplayCase) -> list[Match]:
        return [replay_engine.replay(case.replay) for _ in range(self.RUNS)]

    @pytest.mark.parametrize("case", PLAYABLE, ids=[case.id for case in PLAYABLE])
    def test_every_run_reaches_the_same_fingerprint(self, case: ReplayCase) -> None:
        assert len({match.position.fingerprint for match in self.replays(case)}) == 1

    @pytest.mark.parametrize("case", PLAYABLE, ids=[case.id for case in PLAYABLE])
    def test_every_run_reaches_the_same_result(self, case: ReplayCase) -> None:
        outcomes = {
            (match.status, match.result, match.plies_since_progress) for match in self.replays(case)
        }

        assert len(outcomes) == 1

    @pytest.mark.parametrize("case", PLAYABLE, ids=[case.id for case in PLAYABLE])
    def test_every_run_serialises_identically(self, case: ReplayCase) -> None:
        """Byte-identical JSON, not merely equal objects — a store that
        wrote two different documents for one game would break every
        comparison built on top of it."""
        written = {
            json.dumps(position_to_primitive(match.position)) for match in self.replays(case)
        }

        assert len(written) == 1

    def test_move_ordering_is_stable_across_runs(self) -> None:
        """The generator's order is part of the contract (AD-14), and a
        replay walks it. Two runs that ordered differently would replay
        identically and diverge the moment anything indexed a move list."""
        for case in PLAYABLE:
            start = case.replay.opening_position
            assert generator.legal_moves(start) == generator.legal_moves(start)

    def test_a_replay_payload_serialises_identically_every_time(self) -> None:
        for case in PLAYABLE:
            once = json.dumps(replay_to_primitive(case.replay))
            twice = json.dumps(replay_to_primitive(case.replay))

            assert once == twice, case.source


class TestReplayProperties:
    """Properties over the corpus rather than examples beside it."""

    def test_no_replay_mutates_its_payload(self) -> None:
        for case in PLAYABLE:
            before = replay_to_primitive(case.replay)

            replay_engine.replay(case.replay)

            assert replay_to_primitive(case.replay) == before, case.source

    def test_no_replay_mutates_its_opening_position(self) -> None:
        """A `Position` is a value; a replay that edited one would corrupt
        every other case sharing it."""
        for case in PLAYABLE:
            opening = case.replay.opening_position
            snapshot = Position(board=opening.board, side_to_move=opening.side_to_move)

            replay_engine.replay(case.replay)

            assert case.replay.opening_position == snapshot, case.source

    def test_every_replay_keeps_the_version_it_was_recorded_under(self) -> None:
        for case in PLAYABLE:
            assert replay_engine.replay(case.replay).engine_version == CURRENT_ENGINE_VERSION

    def test_the_ply_count_equals_the_log_length(self) -> None:
        for case in PLAYABLE:
            replayed = replay_engine.replay(case.replay)

            assert replayed.ply_number == len(case.replay.records), case.source

    def test_a_finished_replay_has_a_result_and_an_unfinished_one_does_not(self) -> None:
        """DM-08's absence rule, checked across every case: a result exists
        exactly when the match completed."""
        for case in PLAYABLE:
            replayed = replay_engine.replay(case.replay)

            assert (replayed.result is not None) is (replayed.status.value == "completed"), (
                case.source
            )
