"""The `game` domain — entities, value objects and invariants.

A64-014.6 builds the rules-facing core of `Match`; the module docstring
lists what the aggregate is still missing and why.
"""

from app.modules.game.domain.draws import DrawReason, DrawRuleSet, MatchHistory
from app.modules.game.domain.exceptions import InvalidMatchTransition
from app.modules.game.domain.match import Match, MatchStatus
from app.modules.game.domain.result import MatchOutcome, MatchResult, TerminationReason

__all__ = [
    "DrawReason",
    "DrawRuleSet",
    "InvalidMatchTransition",
    "MatchHistory",
    "Match",
    "MatchOutcome",
    "MatchResult",
    "MatchStatus",
    "TerminationReason",
]
