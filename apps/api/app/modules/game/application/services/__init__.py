"""`game`'s use cases — A64-015.4.

Four services, and the split between them is by *capability* rather than by
aggregate, which is the argument every port pair on this platform makes:

    PersistentMatchCreation  brings a match into existence, idempotently
    MatchAcceptanceService   collects the two answers, and expires the
                             pairings that get none
    GameRecentOpponents      QT-3's rematch guard, as a batch read
    GamePairingSettlements   did this reserved ticket produce a match

Each satisfies one published port and holds only what that port needs, so a
route handling an accept cannot expire anybody's match and a pairing scan
cannot read a match's acceptance state.
"""

from app.modules.game.application.services.match_acceptance_service import (
    MatchAcceptanceService,
    view_of,
)
from app.modules.game.application.services.match_creation_service import (
    PersistentMatchCreation,
)
from app.modules.game.application.services.pairing_settlement_service import (
    GamePairingSettlements,
)
from app.modules.game.application.services.recent_opponent_service import (
    GameRecentOpponents,
)

__all__ = [
    "GamePairingSettlements",
    "GameRecentOpponents",
    "MatchAcceptanceService",
    "PersistentMatchCreation",
    "view_of",
]
