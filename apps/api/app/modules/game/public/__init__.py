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

`events.py` — the five durable match events. R-3 requires downstream
    modules to "subscribe to its events", and a subscriber that cannot name
    the event has to match on a string literal — A64-015.5.

`metrics.py` — `MATCH_ANSWER_LATENCY`, `MATCH_OUTCOMES` and their label
    sets. Published because the setting they inform
    (`MATCHMAKING_RESERVATION_TTL_SECONDS`) belongs to another module —
    A64-015.5 §7.

`retention.py` — `AbandonedMatchRetention`. Deleting the pairings that
    never became games. `game` owns the rows; the horizon is the same
    product judgement as the queue's, so the module with the opinion drives
    the sweep — A64-015.5 §8.

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

from app.modules.game.domain.variants import (
    ProductVariant,
    VariantNotOffered,
    board_variant_of,
    is_offered,
    require_offered,
    variant_catalogue,
)
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
from app.modules.game.public.events import (
    MATCH_AGGREGATE,
    MatchAcceptanceExpired,
    MatchAcceptedByPlayer,
    MatchActivated,
    MatchCreated,
    MatchDeclined,
)
from app.modules.game.public.matches import (
    CreateMatchRequest,
    CreateMatchResult,
    MatchCreationRefused,
    MatchCreationUseCase,
    MatchParticipant,
    PlayerSide,
)
from app.modules.game.public.metrics import (
    MATCH_ANSWER_LATENCY,
    MATCH_OUTCOMES,
    AnswerLatency,
    MatchOutcome,
)
from app.modules.game.public.opponents import RecentOpponentReader
from app.modules.game.public.reconciliation import (
    PairingReconciliationReader,
    PairingSettlement,
)
from app.modules.game.public.retention import AbandonedMatchRetention
from app.modules.game.public.rooms import MatchRoster, MatchRosterReader

__all__ = [
    "MATCH_AGGREGATE",
    "AbandonedMatchRetention",
    "MATCH_ANSWER_LATENCY",
    "MATCH_OUTCOMES",
    "AcceptanceWindowClosed",
    "AnswerLatency",
    "CreateMatchRequest",
    "CreateMatchResult",
    "GameEngineServices",
    "MatchAcceptanceExpired",
    "MatchAcceptanceExpiryUseCase",
    "MatchAcceptanceUseCase",
    "MatchAcceptedByPlayer",
    "MatchActivated",
    "MatchCreated",
    "MatchCreationRefused",
    "MatchCreationUseCase",
    "MatchDeclined",
    "MatchNotFound",
    "MatchNotPending",
    "MatchOutcome",
    "MatchParticipant",
    "MatchRecordStatus",
    "MatchRoster",
    "MatchRosterReader",
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
