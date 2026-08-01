"""The engine collaborators `game` shares, and the version it stamps —
A64-015.2.

Split out of `public/__init__.py` by A64-015.3 alongside `variants.py`;
nothing here changed in the move.
"""

from dataclasses import dataclass
from functools import lru_cache

from app.modules.engine import (
    CURRENT_ENGINE_VERSION,
    EngineVersion,
    MoveApplier,
    MoveGenerator,
    MoveValidator,
    TerminalStateEvaluator,
)
from app.modules.game.domain import DrawRuleSet, ReplayEngine


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


__all__ = ["GameEngineServices", "engine_services", "game_engine_version"]
