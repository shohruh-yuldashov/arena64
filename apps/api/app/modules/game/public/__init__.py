"""`game`'s published surface — BE-03, architecture.md R-1.

The **only** package another module may import. A64-015.2 gives it its
first contents, because `matchmaking` is the first module that needs
something from `game`: which variants a player may choose, and which rules
build a game would be created under.

## What is published, and what deliberately is not

| Published | Why |
| --- | --- |
| `ProductVariant` | The variants a *player* may select — a strict subset of
| | the engine's `BoardVariant` |
| `variant_catalogue()` | The list a client renders and an API validates against |
| `board_variant_of()` | The one place a product choice becomes an engine rule set |
| `game_engine_version()` | AD-15 — a match records the rules it was played under, and
| | matchmaking will stamp it on a match request |
| `GameEngineServices` | One shared, stateless instance of each engine collaborator |

Not published: `Match`, `MatchStatus`, `MatchResult`, the move log,
`ReplayData`. R-3 is explicit that the modules which care about matches
"**never call into `game` to change anything**; they subscribe to its
events", and architecture.md §7 draws only four inbound edges, each a
narrow port. Publishing the aggregate would let a consumer take a
dependency none of those edges intends — and `matchmaking`'s edge points
the *other* way: it will ask `game` to create a match, which is a command
this module will accept, not a type it hands out.

## Why `ProductVariant` is not `BoardVariant`

`BoardVariant` has three members and one of them, `ENGLISH_8X8`, is a
**testing and configuration fixture** rather than a product (recorded in
`specs/game-engine/audit.md` §9). It exists because it is the only second
value three rule axes have, and because the published English draughts
perft series is the engine's only external oracle — so it cannot be
deleted, and it must not be offered.

A validator on a `BoardVariant` field would keep it out of *responses* and
still publish it in the OpenAPI schema as an accepted value. A separate
enum keeps it out of both.

The two are **not** two identifiers: every `ProductVariant` value is a
`BoardVariant` value, `board_variant_of` is the only conversion, and a test
asserts the mapping is total and that `english_8x8` is absent from it.
"""

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from app.core.exceptions import ValidationError
from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    BoardVariant,
    EngineVersion,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    TerminalStateEvaluator,
)
from app.modules.game.domain import DrawRuleSet, ReplayEngine


class ProductVariant(StrEnum):
    """A rule set a player may choose to play.

    Values are `BoardVariant` values, deliberately — one identifier for one
    rule set, so a stored ticket, a wire payload and an engine call all
    spell it the same way.
    """

    RUSSIAN_8X8 = "russian_8x8"
    """The platform's variant. architecture.md A-1's "checkers/draughts with
    mandatory capture and multi-jump moves"."""


class VariantNotOffered(ValidationError):
    """A variant that is not on the menu — A64-015.2.

    A `422` rather than a `404`: the request named something that is not a
    choice, which is malformed input rather than a missing resource. The
    message names the variants that *are* offered, because a client that
    guessed wrong needs the list and there is nothing sensitive in it.
    """


def variant_catalogue() -> tuple[ProductVariant, ...]:
    """Every variant a player may select, in a stable order.

    A tuple rather than the enum itself, so a caller rendering a menu is
    not handed something it could iterate in a different order on a
    different day — and so this can later become a filtered view (a
    variant disabled for maintenance) without every caller changing.
    """
    return tuple(ProductVariant)


def is_offered(variant: str) -> bool:
    """Whether `variant` names something a player may choose."""
    return variant in {member.value for member in ProductVariant}


def require_offered(variant: str) -> ProductVariant:
    """`variant` as a product choice, or `VariantNotOffered`.

    The single gate. `english_8x8` fails here even though the engine plays
    it perfectly well, which is the whole point of the distinction.
    """
    if not is_offered(variant):
        offered = ", ".join(member.value for member in variant_catalogue())
        raise VariantNotOffered(
            f"{variant!r} is not an available variant. Choose one of: {offered}."
        )
    return ProductVariant(variant)


def board_variant_of(variant: ProductVariant) -> BoardVariant:
    """The engine rule set behind a product choice.

    The **only** conversion between the two, so the day a product variant
    maps to something other than its like-named `BoardVariant` there is one
    place it happens.
    """
    return BoardVariant(variant.value)


def game_engine_version() -> EngineVersion:
    """The rules build a game created now would be played under — AD-15.

    Published so `matchmaking` can stamp it on a match request without
    importing the engine, and so the value a match records is read from one
    place. It is a constant, never derived from a date or a build (GE-55).
    """
    return CURRENT_ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class GameEngineServices:
    """One shared instance of every stateless engine collaborator.

    Every one of these holds no state — `MoveGenerator` has no fields,
    `MoveValidator` holds a generator, `MoveApplier` holds a validator —
    so one instance serves the whole process and building them per request
    or per handler is pure waste. `specs/game-engine/audit.md` §14 says so
    directly.

    **Nothing consumes this yet**, and that is stated rather than hidden.
    A64-015.2 creates no match, so no queue code calls the engine. It is
    wired now because the alternative is the first pairing task
    constructing its own collaborators inside a worker or a route handler
    — which is what a composition root exists to prevent, and which is
    much harder to undo than to avoid.

    Frozen, and holding only stateless objects, so sharing it introduces no
    mutable global state.
    """

    generator: MoveGenerator
    validator: MoveValidator
    applier: MoveApplier
    terminal: TerminalStateEvaluator
    draw_rules: DrawRuleSet
    replay: ReplayEngine

    @classmethod
    def create(cls) -> "GameEngineServices":
        """Build the collaborator graph once.

        The wiring order is the dependency order: a validator needs a
        generator, an applier needs a validator, a replay engine needs the
        applier, the terminal evaluator and the draw rules. Assembled here
        rather than in `app_factory` so the shape lives with the module
        that owns it.
        """
        generator = MoveGenerator()
        validator = MoveValidator(generator)
        applier = MoveApplier(validator)
        terminal = TerminalStateEvaluator(generator)
        draw_rules = DrawRuleSet()
        return cls(
            generator=generator,
            validator=validator,
            applier=applier,
            terminal=terminal,
            draw_rules=draw_rules,
            replay=ReplayEngine(applier, terminal, draw_rules),
        )


@lru_cache(maxsize=1)
def engine_services() -> GameEngineServices:
    """The process-wide engine collaborators.

    Cached rather than assigned to a module global, so the sharing is
    explicit at the call site and a test can clear it. `app_factory` calls
    this once at startup; nothing else should need to.
    """
    return GameEngineServices.create()


__all__ = [
    "GameEngineServices",
    "ProductVariant",
    "VariantNotOffered",
    "board_variant_of",
    "engine_services",
    "game_engine_version",
    "is_offered",
    "require_offered",
    "variant_catalogue",
]
