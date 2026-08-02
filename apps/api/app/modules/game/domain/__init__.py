"""The `game` domain — entities, value objects and invariants.

A64-014.6 builds the rules-facing core of `Match`; the module docstring
lists what the aggregate is still missing and why.
"""

from app.modules.game.domain.draws import DrawReason, DrawRuleSet, MatchHistory
from app.modules.game.domain.events import (
    MatchAcceptanceExpired,
    MatchAcceptedByPlayer,
    MatchActivated,
    MatchCreated,
    MatchDeclined,
)
from app.modules.game.domain.exceptions import (
    AcceptanceWindowClosed,
    CorruptMoveLog,
    InvalidMatchTransition,
    MalformedMoveLog,
    MatchNotFound,
    MatchNotPending,
    NotAMatchParticipant,
    PositionHashMismatch,
    ReplayError,
    ReplayResultMismatch,
    UnsupportedEngineVersion,
)
from app.modules.game.domain.match import Match, MatchStatus
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.domain.move_log import MoveRecord
from app.modules.game.domain.replay import SUPPORTED_ENGINE_VERSIONS, ReplayData, ReplayEngine
from app.modules.game.domain.result import MatchOutcome, MatchResult, TerminationReason

__all__ = [
    "SUPPORTED_ENGINE_VERSIONS",
    "AcceptanceWindowClosed",
    "CorruptMoveLog",
    "DrawReason",
    "DrawRuleSet",
    "InvalidMatchTransition",
    "MalformedMoveLog",
    "Match",
    "MatchAcceptanceExpired",
    "MatchAcceptedByPlayer",
    "MatchActivated",
    "MatchCreated",
    "MatchDeclined",
    "MatchHistory",
    "MatchNotFound",
    "MatchNotPending",
    "MatchOutcome",
    "MatchRecord",
    "MatchRecordStatus",
    "MatchResult",
    "MatchSeat",
    "MatchStatus",
    "MoveRecord",
    "NotAMatchParticipant",
    "PositionHashMismatch",
    "ReplayData",
    "ReplayEngine",
    "ReplayError",
    "ReplayResultMismatch",
    "TerminationReason",
    "UnsupportedEngineVersion",
]
