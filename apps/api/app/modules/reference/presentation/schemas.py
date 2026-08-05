"""Wire schema for the time control catalogue.

One model, mapping from `reference.public.TimeControl`. Nothing here is a
domain type: the value carries a nested snapshot, and what a client receives
is the same information flattened into the shape a `<select>` renders from.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.modules.rating.public import SpeedClass
from app.modules.reference.public import TimeControl, TimeControlId


class TimeControlResponse(BaseModel):
    """One control a player may choose.

    **Flat, not nested.** `TimeControl` composes a `TimeControlSnapshot`
    because the platform needs to talk about "the durable subset a record
    copies" — a distinction that means nothing to a client, which renders
    every field at once. Publishing the nesting would export an internal
    reason for a split into a contract that has no use for it.

    `is_active` is **absent**, and its absence is the contract: this
    endpoint returns only active controls, so a field that is `true` on
    every row would invite a client to filter on something already filtered
    and to render a retired control the day the invariant changed.
    """

    model_config = ConfigDict(extra="forbid")

    id: TimeControlId = Field(
        description=(
            "The stable code to send as `time_control_id` when joining a queue. "
            "Never assemble one from a duration — the catalogue is authoritative "
            "and a control can be retired."
        )
    )

    label: str = Field(
        description=(
            'What to show, as the platform spells it — "3+2". A convenience for '
            "a client with no formatter; `base_time_ms` and `increment_ms` are "
            "the values to format from if you have one."
        )
    )

    base_time_ms: int = Field(description="Each side's starting budget, in milliseconds.")
    increment_ms: int = Field(
        description="What a side gets back after each of its own moves, in milliseconds."
    )

    speed_class: SpeedClass = Field(
        description=(
            "Which rating a result under this control moves. Sent so a client "
            "can group or label the menu without owning a duration-to-speed rule "
            "of its own — that mapping is this catalogue's, and the boundaries "
            "between the classes are a product decision rather than arithmetic."
        )
    )

    @classmethod
    def of(cls, control: TimeControl) -> "TimeControlResponse":
        return cls(
            id=control.id,
            label=control.label,
            base_time_ms=control.base_time_ms,
            increment_ms=control.increment_ms,
            speed_class=control.speed_class,
        )


__all__ = ["TimeControlResponse"]
