"""`rating.updated` — what this module tells the rest of the platform.

R-4's chain is `game -> rating -> leaderboard`, one way. This is the second
arrow, and it is an event rather than a call for the reason the first one is:
nothing waits for a leaderboard, and a leaderboard that failed must never be
able to fail a rating.

## What it carries, and what it deliberately does not

Everything a consumer needs to act without reading anything back — the key,
the triple before and after, the delta, and the provisional mark — because
`services.md` §10.2 makes payloads self-contained and because by the time a
relay delivers this the rating may have moved again.

**No match detail beyond its id.** Not the outcome, not the opponent, not the
move count. A leaderboard does not need them, and an event carrying a game's
shape would make every future consumer of a *rating* change a consumer of
game data — which is how a bounded context stops being one.
"""

from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from app.platform.events import DomainEvent

#: The aggregate these events are about.
RATING_AGGREGATE = "player_rating"


@dataclass(frozen=True)
class RatingUpdated(DomainEvent):
    """One player's rating moved, because one match completed."""

    event_type: ClassVar[str] = "rating.updated"
    aggregate_type: ClassVar[str] = RATING_AGGREGATE

    player_id: UUID
    match_id: UUID
    variant: str
    speed_class: str

    rating_before: float
    rating_after: float
    deviation_after: float
    volatility_after: float

    games_played: int
    is_provisional: bool
    algorithm_version: str

    @property
    def aggregate_id(self) -> UUID:
        return self.player_id

    @property
    def delta(self) -> float:
        """How far the rating moved. Negative for a loss.

        Derived rather than stored on the payload as a fourth number that
        could disagree with the two it comes from — it is computed once,
        below, where it is serialised.
        """
        return self.rating_after - self.rating_before

    def payload(self) -> dict[str, Any]:
        return {
            "player_id": str(self.player_id),
            "match_id": str(self.match_id),
            "variant": self.variant,
            "speed_class": self.speed_class,
            "rating_before": self.rating_before,
            "rating_after": self.rating_after,
            "delta": self.delta,
            "deviation_after": self.deviation_after,
            "volatility_after": self.volatility_after,
            "games_played": self.games_played,
            "is_provisional": self.is_provisional,
            "algorithm_version": self.algorithm_version,
        }


__all__ = ["RATING_AGGREGATE", "RatingUpdated"]
