"""Wire shape for a player's rating over time — A64-032.

Built from `rating.public.RatingHistory`, never from ORM rows. Each point is
the published `RatingChange` — three integers, rounded once, with `delta`
served rather than left to the client — plus when and in which match, so a
chart point can say which game moved it.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.rating.public import RatingHistory, RatingKey


class RatingHistoryPointResponse(BaseModel):
    match_id: UUID
    at: datetime = Field(description="When the adjustment was applied.")
    before: int
    after: int
    delta: int = Field(description="`after - before`, computed once, on the server.")


class RatingHistoryResponse(BaseModel):
    """One key's adjustments, oldest first, capped at the most recent `limit`."""

    variant: str
    speed_class: str
    points: list[RatingHistoryPointResponse]
    has_earlier: bool = Field(
        description=(
            "Older adjustments exist beyond the limit, so the first point is not the first game."
        )
    )

    @classmethod
    def of(cls, key: RatingKey, history: RatingHistory) -> "RatingHistoryResponse":
        return cls(
            variant=key.variant.value,
            speed_class=key.speed_class.value,
            points=[
                RatingHistoryPointResponse(
                    match_id=point.match_id,
                    at=point.applied_at,
                    before=point.change.before,
                    after=point.change.after,
                    delta=point.change.delta,
                )
                for point in history.points
            ],
            has_earlier=history.has_earlier,
        )


__all__ = ["RatingHistoryPointResponse", "RatingHistoryResponse"]
