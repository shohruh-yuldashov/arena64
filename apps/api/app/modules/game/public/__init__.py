"""`game`'s published surface — BE-03, architecture.md R-1.

The **only** package another module may import. A64-015.2 gave it its first
contents, because `matchmaking` was the first module that needed something
from `game`; A64-015.3 adds the command that edge exists for.

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
    `MatchCreationUnavailable`, `UnavailableMatchCreation`, `PlayerSide`.
    The "creates match" edge architecture.md §7 draws from `matchmaking`.

## What is deliberately not published

`Match`, `MatchStatus`, `MatchResult`, the move log, `ReplayData`. R-3 is
explicit that the modules which care about matches "**never call into
`game` to change anything**; they subscribe to its events", and publishing
the aggregate would let a consumer take a dependency none of
architecture.md §7's four inbound edges intends.

`matchmaking`'s edge is the one that points inward, and A64-015.3 answers
it the way R-3 permits: with a **command** it accepts, not a type it hands
out. A caller can ask for a match to exist; it cannot advance one.
"""

from app.modules.game.public.engine_services import (
    GameEngineServices,
    engine_services,
    game_engine_version,
)
from app.modules.game.public.matches import (
    CreateMatchRequest,
    CreateMatchResult,
    MatchCreationRefused,
    MatchCreationUnavailable,
    MatchCreationUseCase,
    MatchParticipant,
    PlayerSide,
    UnavailableMatchCreation,
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
    "CreateMatchRequest",
    "CreateMatchResult",
    "GameEngineServices",
    "MatchCreationRefused",
    "MatchCreationUnavailable",
    "MatchCreationUseCase",
    "MatchParticipant",
    "PlayerSide",
    "ProductVariant",
    "UnavailableMatchCreation",
    "VariantNotOffered",
    "board_variant_of",
    "engine_services",
    "game_engine_version",
    "is_offered",
    "require_offered",
    "variant_catalogue",
]
