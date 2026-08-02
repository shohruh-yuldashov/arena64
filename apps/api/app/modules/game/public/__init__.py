"""`game`'s published surface — BE-03, architecture.md R-1.

The **only** package another module may import. A64-015.2 gave it its first
contents, because `matchmaking` was the first module that needed something
from `game`; A64-015.3 added the command that edge exists for; A64-015.4
adds the three reads and the second command that turn a created match into
an accepted one.

The types live in submodules and this file re-exports them, which is the
shape `friends/public/` already uses — one file per question, one import
site for consumers.

`variants.py` — `ProductVariant`, `variant_catalogue`, `is_offered`,
    `require_offered`, `board_variant_of`. Which rule sets a *player* may
    choose, and the one conversion to the engine's `BoardVariant`.

`engine_services.py` — `game_engine_version`, `GameEngineServices`,
    `engine_services`. AD-15's version stamp, and one shared instance of
    each stateless engine collaborator.

`matches.py` — `CreateMatchRequest`, `CreateMatchResult`,
    `MatchParticipant`, `MatchCreationUseCase`, `MatchCreationRefused`,
    `PlayerSide`. The "creates match" edge architecture.md §7 draws from
    `matchmaking`.

`acceptance.py` — `PendingMatchView`, `MatchAcceptanceUseCase`,
    `MatchAcceptanceExpiryUseCase`, `MatchRecordStatus` and the three
    refusals. The second half of that same edge: a pairing is finished when
    both players have answered.

`opponents.py` — `RecentOpponentReader`. QT-3's rematch guard, which
    A64-015.3 declared and could not implement.

`reconciliation.py` — `PairingSettlement`, `PairingReconciliationReader`.
    The one fact `matchmaking` cannot hold: did this reserved ticket's
    match get created.

## What is deliberately not published

`Match`, `MatchRecord`, `MatchStatus`, `MatchResult`, the move log,
`ReplayData`. R-3 is explicit that the modules which care about matches
"**never call into `game` to change anything**; they subscribe to its
events", and publishing an aggregate would let a consumer take a dependency
none of architecture.md §7's four inbound edges intends.

`matchmaking`'s edge is the one that points inward, and it is answered the
way R-3 permits: with **commands `game` accepts** and **views it hands
out**, never a type that can advance a game. `PendingMatchView` is a
projection of `MatchRecord` with the pairing internals removed — no
`pairing_id`, no queue ticket ids — for the same reason.

`MatchRecordStatus` is the one domain type re-exported, and it is a closed
enum on a published view rather than a capability: a client has to be able
to tell "still waiting" from "your opponent declined", and a second enum
mirroring it would be two places that answer diverges.
"""

from app.modules.game.public.acceptance import (
    AcceptanceWindowClosed,
    MatchAcceptanceExpiryUseCase,
    MatchAcceptanceUseCase,
    MatchNotFound,
    MatchNotPending,
    MatchRecordStatus,
    NotAMatchParticipant,
    PendingMatchView,
)
from app.modules.game.public.engine_services import (
    GameEngineServices,
    engine_services,
    game_engine_version,
)
from app.modules.game.public.matches import (
    CreateMatchRequest,
    CreateMatchResult,
    MatchCreationRefused,
    MatchCreationUseCase,
    MatchParticipant,
    PlayerSide,
)
from app.modules.game.public.opponents import RecentOpponentReader
from app.modules.game.public.reconciliation import (
    PairingReconciliationReader,
    PairingSettlement,
)
from app.modules.game.public.variants import (
    ProductVariant,
    VariantNotOffered,
    board_variant_of,
    is_offered,
    require_offered,
    variant_catalogue,
)

__all__ = [
    "AcceptanceWindowClosed",
    "CreateMatchRequest",
    "CreateMatchResult",
    "GameEngineServices",
    "MatchAcceptanceExpiryUseCase",
    "MatchAcceptanceUseCase",
    "MatchCreationRefused",
    "MatchCreationUseCase",
    "MatchNotFound",
    "MatchNotPending",
    "MatchParticipant",
    "MatchRecordStatus",
    "NotAMatchParticipant",
    "PairingReconciliationReader",
    "PairingSettlement",
    "PendingMatchView",
    "PlayerSide",
    "ProductVariant",
    "RecentOpponentReader",
    "VariantNotOffered",
    "board_variant_of",
    "engine_services",
    "game_engine_version",
    "is_offered",
    "require_offered",
    "variant_catalogue",
]
