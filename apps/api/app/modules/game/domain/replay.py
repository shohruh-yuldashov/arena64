"""`ReplayData` and `ReplayEngine` — reconstructing a game from its record.

Framework-free (architecture.md §8). No Pydantic, no ORM, no I/O: a replay
payload is a frozen value object and the engine that reads one is a pure
function of it.

## What a replay has to reproduce

Not the final board. **Why the game ended.**

A replay that rebuilt only the position would agree about where the pieces
stopped and have nothing to say about a draw by repetition, which is a
property of the whole sequence. So this applies every ply through
`Match.play` — the same validator, the same applier, the same terminal
evaluator, the same draw rules a live game uses — and the position counts
and the no-progress counter come back because they are *recomputed*, never
restored.

That is the rule this module exists to enforce: **one source of truth for
what a game is.** A replay that took a shortcut would be a second
implementation of the rules, and it would disagree with the first exactly
where a disputed game needs them to agree.

## Verification is per ply, not at the end

Each record carries the fingerprint of the position it produced, and the
replay checks it as it goes. A mismatch caught on the ply that caused it
names the move whose semantics changed; the same mismatch caught at the
end names nothing. That is the difference between "a rules fix moved this
game" and "this game is wrong somewhere".

## Historical versions

`SUPPORTED_ENGINE_VERSIONS` is an explicit set, and it currently holds
**version 2 only**.

Version 1 had no draw rules (A64-014.7 added them and bumped the version
for exactly this reason). A version-1 game replayed under version 2 could
end earlier than it did — a repetition that ran on in the real game would
draw in the replay — which is AD-15's scenario word for word. So a
version-1 replay is **refused**, not approximated.

Nothing has been persisted under version 1: no store exists yet, so the
refusal costs nothing today and is the honest position when it does. The
seam is a set rather than an `if`, so supporting an older build means
adding a rules profile beside the current one rather than editing this.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    BoardVariant,
    EngineVersion,
    IllegalMove,
    MoveApplier,
    Position,
    TerminalStateEvaluator,
)
from app.modules.game.domain.draws import DrawRuleSet
from app.modules.game.domain.exceptions import (
    CorruptMoveLog,
    InvalidMatchTransition,
    MalformedMoveLog,
    PositionHashMismatch,
    ReplayResultMismatch,
    UnsupportedEngineVersion,
)
from app.modules.game.domain.match import Match, MatchStatus
from app.modules.game.domain.move_log import MoveRecord
from app.modules.game.domain.result import MatchResult

SUPPORTED_ENGINE_VERSIONS: frozenset[EngineVersion] = frozenset({CURRENT_ENGINE_VERSION})
"""The rules builds this one can faithfully reproduce — see the module
docstring on why that is not "all of them"."""


@dataclass(frozen=True, slots=True)
class ReplayData:
    """Everything needed to reconstruct one game, and nothing derived.

    Note what is **absent**: no position counts, no no-progress counter, no
    final board. Every one of those is recomputed by replaying the log, and
    storing them would create a second answer to a question the moves
    already settle — one that a rules fix would silently leave stale.
    """

    engine_version: EngineVersion
    """Explicit, always. Never inferred from a date, a schema version, or
    the version this build happens to be — see
    `engine.serialization.engine_version_from_primitive`."""

    variant: BoardVariant
    opening_position: Position
    records: Sequence[MoveRecord] = field(default_factory=tuple)

    expected_result: MatchResult | None = None
    """What the record says the game ended as, if it says.

    Optional because a replay of a game still in progress has no result to
    check against. When present it is checked, and a mismatch is an error
    rather than a note — a replay that ended differently from the game is
    not a replay of that game.
    """


class ReplayEngine:
    """Rebuilds a `Match` from its record, through the live rules."""

    def __init__(
        self,
        applier: MoveApplier,
        evaluator: TerminalStateEvaluator,
        draw_rules: DrawRuleSet,
        supported_versions: frozenset[EngineVersion] = SUPPORTED_ENGINE_VERSIONS,
    ) -> None:
        self._applier = applier
        self._evaluator = evaluator
        self._draw_rules = draw_rules
        self._supported_versions = supported_versions

    def replay(self, replay: ReplayData) -> Match:
        """The match `replay` describes, played out ply by ply.

        Raises `UnsupportedEngineVersion` before touching a move,
        `MalformedMoveLog` for a log that is not contiguous from 1,
        `CorruptMoveLog` for a move the rules refuse or one recorded after
        the game ended, `PositionHashMismatch` on the first ply whose
        result disagrees with the record, and `ReplayResultMismatch` if a
        stated result is not the one reached.
        """
        self._require_supported(replay.engine_version)
        _require_contiguous(replay.records)

        match = Match(
            variant=replay.variant,
            engine_version=replay.engine_version,
            position=replay.opening_position,
        )
        match.start()

        for record in replay.records:
            self._apply(match, record)

        _require_expected_result(match, replay.expected_result)
        return match

    def _require_supported(self, version: EngineVersion) -> None:
        if version not in self._supported_versions:
            supported = ", ".join(str(known) for known in sorted(self._supported_versions))
            raise UnsupportedEngineVersion(
                f"{version} cannot be replayed by this build; it reproduces {supported}."
            )

    def _apply(self, match: Match, record: MoveRecord) -> None:
        try:
            match.play(record.move, self._applier, self._evaluator, self._draw_rules)
        except (IllegalMove, InvalidMatchTransition) as refusal:
            # Chained, because *which* rule refused it is the diagnostic —
            # and in a replay it means the record is wrong rather than that
            # a player was told no.
            raise CorruptMoveLog(
                f"Ply {record.ply_number} ({record.move}) is not playable here: {refusal.message}"
            ) from refusal

        if match.position.fingerprint != record.resulting_position_hash:
            raise PositionHashMismatch(
                f"Ply {record.ply_number} ({record.move}) produced "
                f"{match.position.fingerprint!r}, and the record says "
                f"{record.resulting_position_hash!r}."
            )


def _require_contiguous(records: Sequence[MoveRecord]) -> None:
    """MT-5: ply numbers are contiguous from 1.

    Checked as a whole before anything is applied, so a truncated or
    duplicated log is refused outright rather than half-replayed into a
    position that never occurred.
    """
    for expected, record in enumerate(records, start=1):
        if record.ply_number != expected:
            raise MalformedMoveLog(
                f"The move log jumps to ply {record.ply_number} where {expected} was expected."
            )


def _require_expected_result(match: Match, expected: MatchResult | None) -> None:
    if expected is None:
        return
    if match.status is not MatchStatus.COMPLETED or match.result != expected:
        raise ReplayResultMismatch(
            f"The record says the match ended {expected}, and the replay reached "
            f"{match.result if match.result is not None else match.status.value}."
        )


__all__ = ["SUPPORTED_ENGINE_VERSIONS", "ReplayData", "ReplayEngine"]
