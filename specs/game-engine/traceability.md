# Game Engine — Specification Traceability

> **Status:** Current as of A64-014.10
> **Owner:** _Unassigned_
> **Last audited:** 2026-08-02
> **Covers:** GE-1 – GE-101 in `specs/game-engine.md`
> **Related:** `specs/game-engine/audit.md`

Every numbered rule in the engine specification, the code that implements it, and the evidence
that it holds. Paths are relative to `apps/api/`.

## Status vocabulary

| Status | Meaning |
| --- | --- |
| **VERIFIED** | Implemented, and a test or corpus case fails if it stops being true |
| **PARTIALLY VERIFIED** | Implemented and evidenced, but some part of the claim rests on convention rather than on a check |
| **NOT VERIFIED** | Implemented with no evidence, or claimed with no implementation |
| **DEFERRED** | The rule records something deliberately not done yet |
| **SUPERSEDED** | A later task retired the rule; the number is not reused |
| **PRODUCT DECISION REQUIRED** | Blocked on a decision nobody has made |

## Summary

| Status | Count |
| --- | --- |
| VERIFIED | 96 |
| PARTIALLY VERIFIED | 1 |
| DEFERRED | 1 |
| SUPERSEDED | 2 |
| NOT VERIFIED | **0** |
| **Total numbered** | **101** |

GE-28 and GE-29 are permanent gaps in the numbering, not omissions: A64-014.5 retired the
king-refusal rules and `specs/game-engine.md` §3.4 keeps them as prose. Numbers are never reused.

---

## §1 Board foundation — A64-014.1

| Rule | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| GE-1 | `engine/coordinate.py`, `variant.forward_step` / `promotion_row` | `test_board_coordinate.py::TestReadableRepresentation`, `test_board_variant.py::TestRuleAxes`, `test_initial_position.py` | VERIFIED |
| GE-2 | `variant.BoardGeometry.is_playable` | `test_board_variant.py::TestPlayableSquares` | VERIFIED |
| GE-3 | `variant.BoardGeometry.__post_init__` | `test_board_variant.py::TestGeometryRefusals` | VERIFIED |
| GE-4 | `coordinate.MAX_BOARD_DIMENSION` | `test_board_variant.py::test_no_variant_exceeds_the_addressable_board` | VERIFIED |
| GE-5 | `initial_position.initial_board` | `test_initial_position.py`, corpus `russian-initial-position-light-to-move` | VERIFIED |
| GE-6 | `initial_position.initial_board` | `test_initial_position.py::test_nobody_starts_with_a_king` | VERIFIED |
| GE-7 | `initial_position`, `BoardGeometry.__post_init__` | `test_initial_position.py::test_only_playable_squares_are_occupied` | VERIFIED |

## §2 Men's move generation — A64-014.2

| Rule | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| GE-8 | `engine/position.py` | `test_position.py`, `test_match_lifecycle.py::test_the_counter_lives_on_the_match_and_not_the_position` | VERIFIED |
| GE-9 | `position.Position` | `test_position.py::TestValueEquality` | VERIFIED |
| GE-10 | `position.Position.fingerprint` | `test_position.py::TestFingerprint` | VERIFIED |
| GE-11 | `engine/move.py` | `test_move.py::TestPathInvariants` | VERIFIED |
| GE-12 | `move.Move`, `move_generation._quiet_moves` | `test_move.py`, corpus `light-man-quiet-moves-both-diagonals` | VERIFIED |
| GE-13 | `move.Move.__post_init__` | `test_move.py`, `test_capture_sequences.py::TestTakenPiecesStayStanding` | VERIFIED |
| GE-14 | `move.Move.__post_init__` | `test_move.py::test_a_path_may_revisit_a_square_it_did_not_just_leave` | VERIFIED |
| GE-15 | `move.Move`, `move_generation._promotion` | `test_move_generation.py::test_the_moving_piece_is_not_mutated` | VERIFIED |
| GE-16 | `move.Move.sort_key` | `test_move.py::TestOrdering` | VERIFIED |
| GE-17 | `move_generation.legal_moves` | corpus `capture-suppresses-every-other-piece-quiet-moves` | VERIFIED |
| GE-18 | `move_generation.legal_moves`, `_obliged` | `test_capture_sequences.py::test_the_filter_runs_after_the_search_and_not_inside_it`, corpus max-capture pair | VERIFIED |
| GE-19 | `move_generation._quiet_moves` | `test_move_generation.py::TestQuietMoves`, fuzz | VERIFIED |
| GE-20 | `move_generation._jumps_from` | `test_move_generation.py::TestSingleCaptures`, corpus | VERIFIED |
| GE-21 | `move_generation._pieces_to_move` | `test_move_generation.py::TestSideToMove`, fuzz | VERIFIED |
| GE-22 | `move_generation._promotion`, `_sequence_promotion` | `test_move_generation.py::TestPromotion`, `test_king_moves.py` | VERIFIED |
| GE-23 | `move_generation._ordered` | `test_move_generation.py::TestDeterminism`, `test_replay_consistency.py::TestDeterminism`, fuzz | VERIFIED |

## §3 Validation and application — A64-014.3

| Rule | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| GE-24 | `engine/move_validation.py` | `test_move_validation.py::test_every_generated_move_validates` | VERIFIED |
| GE-25 | `move_validation.MoveValidator` | `test_move_validation.py::TestTheValidatorHoldsNoRules` *(added by A64-014.10 — this was the one rule with no evidence)* | VERIFIED |
| GE-26 | `move.Move.__eq__`, `move_validation.is_legal` | `test_move_validation.py::TestPromotionMetadata`, corpus promotion rejections | VERIFIED |
| GE-27 | `move_validation.MoveValidator` | `test_move_validation.py::TestIllegalMoves` | VERIFIED |
| GE-28 | — | Retired by A64-014.5; `specs/game-engine.md` §3.4 | SUPERSEDED |
| GE-29 | — | Retired by A64-014.5; `specs/game-engine.md` §3.4 | SUPERSEDED |
| GE-30 | `move_generation.legal_moves` | `test_king_moves.py::test_a_king_only_position_with_no_moves_answers_empty`, corpus `terminal_positions` | VERIFIED |
| GE-31 | `move_application.MoveApplier.apply` | `test_move_application.py::TestCaptureApplication`, `test_engine_regression.py` | VERIFIED |
| GE-32 | `move_application.MoveApplier.apply` | `test_move_application.py::TestRefusals` | VERIFIED |
| GE-33 | `move_application`, immutable values | `test_move_application.py::TestNothingIsMutated`, fuzz | VERIFIED |
| GE-34 | `move_application.MoveApplier.apply` | `test_move_application.py::TestQuietMoveApplication` | VERIFIED |
| GE-35 | `move_application` | `test_move_application.py::TestDeterminism` | VERIFIED |

## §4 Complete capture sequences — A64-014.4

| Rule | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| GE-36 | `move_generation._sequences` | `test_capture_sequences.py::TestCompleteSequences`, corpus `incomplete-prefix-is-not-offered` | VERIFIED |
| GE-37 | `move_generation._sequences` | fuzz (every game terminates), `test_perft.py` | VERIFIED |
| GE-38 | `move_generation._sequences` | `test_capture_sequences.py`, corpus | VERIFIED |
| GE-39 | `move_generation._captures` | `test_capture_sequences.py::TestNothingIsMutated` | VERIFIED |
| GE-40 | `move_generation._jumps_from` | corpus `a-taken-piece-blocks-and-is-never-taken-again` | VERIFIED |
| GE-41 | `move.Move.__post_init__`, `_captures` | corpus (ring case), `test_capture_sequences.py` | VERIFIED |
| GE-42 | `move_generation._sequence_promotion` | `test_capture_sequences.py::TestMidSequencePromotion` | VERIFIED |
| GE-43 | `move_generation._crowns_on_arrival`, `_reach` | corpus `a-promoted-man-carries-on-as-a-flying-king` | VERIFIED |
| GE-44 | `move_generation._obliged` | corpus `maximum-capture-keeps-only-the-longest` | VERIFIED |
| GE-45 | `move_generation._obliged` | `test_capture_sequences.py::TestMaximumCapture` | VERIFIED |

## §5 Kings — A64-014.5

| Rule | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| GE-46 | `variant.BoardGeometry.king_reach` | `test_board_variant.py`, `test_king_moves.py::TestEnglishVariant` | VERIFIED |
| GE-47 | `move_generation._open_squares` | corpus `king-quiet-moves-along-open-diagonals` | VERIFIED |
| GE-48 | `move_generation._open_squares` | corpus `an-opponent-stops-a-king-slide`, `a-friendly-piece-stops-a-king-slide` | VERIFIED |
| GE-49 | `move_generation._promotion`, `_sequence_promotion` | `test_king_moves.py::test_a_king_sliding_across_its_crownhead_is_not_promoted` | VERIFIED |
| GE-50 | `move_generation._jumps_from`, `_landings` | corpus `a-flying-king-capture-offers-every-square-beyond-the-victim` | VERIFIED |
| GE-51 | `move_generation._jumps_from` | `test_king_moves.py::TestKingCaptures` | VERIFIED |
| GE-52 | `move_generation._sequences` | corpus `a-king-capture-changes-direction`, `a-king-cannot-take-the-same-piece-twice` | VERIFIED |
| GE-53 | `move_generation._sequence_promotion` | `test_engine_regression.py::TestAKingThatStartedThePlyIsNotPromoted` | VERIFIED |
| GE-54 | `variant.MidSequencePromotion` | `test_board_variant.py::test_every_mid_sequence_promotion_rule_is_configured_by_some_variant` | VERIFIED |

## §6 Game state and lifecycle — A64-014.6

| Rule | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| GE-55 | `engine/version.py` | `test_terminal_state.py::TestEngineVersion`, `test_serialization.py::TestEngineVersion` | VERIFIED |
| GE-56 | `version.EngineVersion` | `test_terminal_state.py::test_versions_compare` | VERIFIED |
| GE-57 | `game/domain/match.py` | `test_match_lifecycle.py::TestCreation`; no engine code writes the field after construction | **PARTIALLY VERIFIED** — see audit §6 |
| GE-58 | Process rule | Followed at A64-014.7 (version 2); corpus `draw_sequences` and `replays` state `engine_version: 2` | VERIFIED |
| GE-59 | `engine/terminal.py` | `test_terminal_state.py`, corpus `terminal_positions` | VERIFIED |
| GE-60 | `terminal.TerminalStateEvaluator.evaluate` | `test_terminal_state.py::TestTheEvaluatorStaysDrawFree` | VERIFIED |
| GE-61 | `terminal.TerminalStateEvaluator.evaluate` | `test_terminal_state.py::test_running_out_of_pieces_is_reported_ahead_of_having_no_moves` | VERIFIED |
| GE-62 | `terminal.TerminalState` | `test_terminal_state.py::TestPositionsThatContinue` | VERIFIED |
| GE-63 | `terminal.py` (structural) | `test_terminal_state.py::TestEveryVerdictNamesAWinner` | VERIFIED |
| GE-64 | `match.Match` transitions | `test_match_lifecycle.py::TestTransitions` | VERIFIED |
| GE-65 | `match.Match.play` | `test_match_lifecycle.py::TestPlayingAMove` | VERIFIED |
| GE-66 | `match.Match.play` | `test_replay.py::TestTheMoveLog`, fuzz | VERIFIED |
| GE-67 | `match.Match.resign` | `test_match_lifecycle.py::TestResignation` | VERIFIED |
| GE-68 | `match.Match.abort` | `test_match_lifecycle.py::TestAbort` | VERIFIED |
| GE-69 | `game/domain/result.py` | `test_match_lifecycle.py`, fuzz `_check_result_is_coherent` | VERIFIED |
| GE-70 | `position.Position`, `match._position_counts` | `test_position.py`, `test_match_lifecycle.py` | VERIFIED |
| GE-71 | `match.Match.__post_init__` | `test_engine_regression.py::TestTheOpeningPositionCountsAsOccurrenceOne` | VERIFIED |
| GE-72 | `match.Match._record` | fuzz (`sum(position_history.values()) == ply + 1`) | VERIFIED |
| GE-73 | `match.Match.position_history` | `test_match_lifecycle.py::test_the_history_cannot_be_written_through` | VERIFIED |

## §7 Draw rules — A64-014.7

| Rule | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| GE-74 | `engine/terminal.py` unchanged | `test_terminal_state.py::TestTheEvaluatorStaysDrawFree` | VERIFIED |
| GE-75 | `game/domain/draws.py` | `test_draw_rules.py::TestDeterminism` | VERIFIED |
| GE-76 | `position.Position` | `test_position.py` | VERIFIED |
| GE-77 | `draws._has_repeated` | `test_draw_rules.py::TestRepetition` | VERIFIED |
| GE-78 | `match.Match.__post_init__`, `draws._has_repeated` | corpus `threefold-repetition-draws-on-the-second-return`, `two-occurrences-are-not-a-draw` | VERIFIED |
| GE-79 | `match.Match.play` | `test_match_lifecycle.py::test_a_decisive_result_takes_priority_over_a_draw` | VERIFIED |
| GE-80 | `result.MatchResult.__post_init__` | `test_match_lifecycle.py::TestDrawIntegration` | VERIFIED |

## §8 Serialization and replay — A64-014.8

| Rule | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| GE-81 | `engine/serialization.move_to_primitive` | `test_serialization.py::test_two_paths_to_one_destination_serialise_differently` | VERIFIED |
| GE-82 | `engine/serialization.py`, `game/domain/serialization.py` | `test_serialization.py::TestEngineVersion`, `test_corpus_audit.py::TestTheFilesRoundTrip` | VERIFIED |
| GE-83 | `serialization.board_to_primitive` | `test_serialization.py::test_the_squares_are_sorted` | VERIFIED |
| GE-84 | File placement | `.importlinter` `engine-is-a-dependency-free-kernel` | VERIFIED |
| GE-85 | `version.EngineVersion.as_primitive` | `test_serialization.py::test_it_serialises_to_a_plain_integer` | VERIFIED |
| GE-86 | `serialization.engine_version_from_primitive` | `test_serialization.py` (absent, string and boolean all refused) | VERIFIED |
| GE-87 | `match.Match._append`, `replay._require_contiguous` | `test_replay.py::TestReplayRefusals`, fuzz | VERIFIED |
| GE-88 | `match.Match.play` | `test_engine_regression.py::TestARejectedMoveLeavesNoTrace` | VERIFIED |
| GE-89 | `match.Match.move_log`, `MoveRecord` frozen | `test_replay.py::test_editing_the_returned_log_does_not_reach_the_match` | VERIFIED |
| GE-90 | `match.Match.last_move` | `test_replay.py::test_the_last_move_is_read_off_the_log` | VERIFIED |
| GE-91 | `replay.ReplayEngine.replay` | `test_replay_consistency.py`, fuzz `TestEveryRandomGameReplays` | VERIFIED |
| GE-92 | `replay.ReplayEngine._apply` | `test_replay.py::test_a_hash_mismatch_names_the_ply_that_caused_it` | VERIFIED |
| GE-93 | `replay.ReplayEngine.replay` | `test_replay_consistency.py::test_a_replay_agrees_with_the_live_game_at_every_ply` | VERIFIED |

## §9 Verification — A64-014.9

| Rule | Implementation | Evidence | Status |
| --- | --- | --- | --- |
| GE-94 | `tests/perft.py` | `test_perft.py::TestEnglishAgainstPublishedValues` | VERIFIED |
| GE-95 | `tests/perft.py` (no cache, applies through `MoveApplier`) | `test_perft.py::TestPerftItself` | VERIFIED |
| GE-96 | — | `test_perft.py::TestRussianAgainstTheRulesItShares` | VERIFIED |
| GE-97 | — | Records a deferral; the Russian baseline has no external oracle | **DEFERRED** |
| GE-98 | `tests/corpus.py` | `test_corpus_audit.py::TestTheFilesParse` | VERIFIED |
| GE-99 | `tests/corpus.EXPECTATION_KEYS` | `test_corpus_audit.py::test_every_expectation_shape_is_exercised` | VERIFIED |
| GE-100 | `engine/serialization.py` | `test_corpus_audit.py::TestTheFilesRoundTrip` | VERIFIED |
| GE-101 | `tests/corpus.superseded_ids` | `test_corpus_audit.py::TestTheHistoryIsIntact` | VERIFIED |
