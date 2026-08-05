"""The `reference` schema — `time_control`. database.md §6.2, DB-08.

The only place in this module that knows SQLAlchemy exists. Nothing above
`infrastructure/` imports this file, and the values it maps to hold no ORM
type (repositories.md §3).

## No `TimestampMixin`, and no surrogate key

Both are deliberate omissions rather than oversights.

The identity **is** `id` — a `TimeControlId`, the same value that appears in
a pool identifier and on every ticket. A surrogate UUID would be a second
name for one thing, and the pool identifier would then either carry a UUID
(unreadable in a log line) or the code (making the UUID decoration).

`created_at`/`updated_at` would record when a *seed migration* ran, which
answers no question anybody asks. A catalogue is not an event log.

## Why `id` is a native enum and not text

The platform's convention for a closed list, stated at `_enum` in
`rating.infrastructure.models` and applied to `ProductVariant`, `QueueType`,
`Region` and `SpeedClass`: four bytes, and a typo cannot become a value no
read path knows how to evaluate. `TimeControlId` is exactly that kind of
list — see `reference.domain.time_control` on why membership is a code
change.

It also makes the enum and the table unable to disagree about *which*
controls exist. They can still disagree about whether one is seeded, which
is a row this module is entitled to be missing (a withdrawn control) and is
`UnsupportedTimeControl`'s job.

## The check constraints, and what each one stops

Every one restates an invariant `TimeControlSnapshot` also enforces (BE-06),
so a row written by a migration — which does not go through the value
object — cannot be inconsistent either. `base_time_ms > 0` is the one that
matters most: a control with a zero budget is not a fast game, it is a match
every player loses on their first move.
"""

from sqlalchemy import CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.modules.rating.public import SpeedClass
from app.modules.reference.domain.time_control import TimeControlId

#: database.md DB-08 — one schema per bounded context, and this context is
#: the platform's shared catalogue.
REFERENCE_SCHEMA = "reference"


def _enum(python_type: type, name: str) -> PgEnum:
    """A native PostgreSQL enum, spelled the way every other one on this
    platform is — see `rating.infrastructure.models._enum` for the argument.

    `values_callable` stores the member *values* rather than the Python
    member names, and each schema declares its own type: `reference` has its
    own `speed_class` beside `rating`'s rather than borrowing it, so a
    member added by `rating` is not a migration that locks this table.
    """
    return PgEnum(
        python_type,
        name=name,
        schema=REFERENCE_SCHEMA,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


class TimeControlModel(Base):
    """`reference.time_control` — one row per control the platform offers.

    Read on every queue join and by nothing else. Four rows today, and it is
    not expected to reach forty: a menu a player has to scroll is a menu
    that splits the pools it names.
    """

    __tablename__ = "time_control"

    id: Mapped[TimeControlId] = mapped_column(
        _enum(TimeControlId, "time_control_id"), primary_key=True
    )

    label: Mapped[str] = mapped_column(String(32), nullable=False)
    """What a menu calls it — "1+0", "3+2". Free text and editable; nothing
    durable copies it, so correcting one changes no record."""

    base_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    increment_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    """Milliseconds throughout, never seconds — `game.domain.clock` on why:
    a bullet increment is two seconds and a flag margin is tens of
    milliseconds, so a second-resolution column would round a player's
    remaining time to zero while they still had some."""

    speed_class: Mapped[SpeedClass] = mapped_column(
        _enum(SpeedClass, "speed_class"), nullable=False
    )
    """Which rating a game under this control moves — SPEC-RATING §7.1.

    Stored rather than derived from `base_time_ms`, which is the whole point
    of the catalogue: SPEC-RATING OQ-1 leaves the boundaries between bullet,
    blitz and rapid an open product decision, and a derivation would be this
    module guessing them for every rating on the platform.
    """

    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False)
    """Whether it may still be chosen. Retirement, never deletion — the row
    is what a thousand finished matches were played under."""

    __table_args__ = (
        # Not a functional requirement — two controls could legally share a
        # slot and `active()` breaks the tie by identifier. It is a
        # *catalogue* requirement: a menu whose order is ambiguous is one
        # that renders differently on two devices for no reason anybody can
        # see, and the constraint makes that unrepresentable rather than
        # unlikely.
        UniqueConstraint("display_order", name="uq_time_control__display_order"),
        # --- invariants `TimeControlSnapshot` also enforces (BE-06) --------
        CheckConstraint("base_time_ms > 0", name="ck_time_control__base_time_positive"),
        CheckConstraint("increment_ms >= 0", name="ck_time_control__increment_not_negative"),
        CheckConstraint("display_order >= 0", name="ck_time_control__display_order_not_negative"),
        CheckConstraint("length(label) > 0", name="ck_time_control__label_not_blank"),
        {"schema": REFERENCE_SCHEMA},
    )


__all__ = ["REFERENCE_SCHEMA", "TimeControlModel"]
