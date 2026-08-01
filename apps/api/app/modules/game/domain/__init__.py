"""The `game` domain — entities, value objects and invariants.

A64-014.6 builds the rules-facing core of `Match`; the module docstring
lists what the aggregate is still missing and why.
"""

from app.modules.game.domain.draws import DrawReason, DrawRuleSet, MatchHistory
from app.modules.game.domain.exceptions import (
    CorruptMoveLog,
    InvalidMatchTransition,
    MalformedMoveLog,
    PositionHashMismatch,
    ReplayError,
    ReplayResultMismatch,
    UnsupportedEngineVersion,
)
from app.modules.game.domain.match import Match, MatchStatus
from app.modules.game.domain.move_log import MoveRecord
from app.modules.game.domain.replay import SUPPORTED_ENGINE_VERSIONS, ReplayData, ReplayEngine
from app.modules.game.domain.result import MatchOutcome, MatchResult, TerminationReason

__all__ = [
    "SUPPORTED_ENGINE_VERSIONS",
    "CorruptMoveLog",
    "DrawReason",
    "DrawRuleSet",
    "InvalidMatchTransition",
    "MalformedMoveLog",
    "Match",
    "MatchHistory",
    "MatchOutcome",
    "MatchResult",
    "MatchStatus",
    "MoveRecord",
    "PositionHashMismatch",
    "ReplayData",
    "ReplayEngine",
    "ReplayError",
    "ReplayResultMismatch",
    "TerminationReason",
    "UnsupportedEngineVersion",
]
