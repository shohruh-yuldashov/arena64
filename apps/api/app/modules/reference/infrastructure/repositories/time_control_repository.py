"""`reference.public.TimeControlCatalogue` over one session.

Two statements, both single-row-or-handful reads against a table that holds
four rows. There is no join, no aggregate and no pagination, which is why
this file is short and why it stays short: a catalogue that needed a query
planner would not be a catalogue.

## Why this is not cached

Every queue join reads one row here, so the obvious next thought is a
process-lifetime cache of four rows. It is deliberately not built, and
caching.md's rule is the reason: a cache without a documented invalidation
rule is a source of stale data, and the one field that matters —
`is_active` — is edited precisely when somebody needs a control to stop
being offered *now*.

The cost being avoided is one primary-key lookup on a four-row table, on an
endpoint a human pressed, in a request that already does two indexed reads
and a cross-context rating read. Adding a cache here would buy nothing
measurable and would make "retire this control" a deploy.
"""

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.reference.domain.exceptions import UnsupportedTimeControl
from app.modules.reference.domain.time_control import (
    TimeControl,
    TimeControlId,
    TimeControlSnapshot,
)
from app.modules.reference.infrastructure.models import TimeControlModel

logger = logging.getLogger(__name__)


class SqlAlchemyTimeControlCatalogue:
    """The catalogue, read from PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active(self) -> Sequence[TimeControl]:
        """Every offered control, in display order — see the port."""
        rows = (
            await self._session.execute(
                select(TimeControlModel)
                .where(TimeControlModel.is_active.is_(True))
                # `id` after `display_order` makes the order total. The
                # unique constraint means it never actually breaks a tie
                # today; it is here so that dropping the constraint later
                # cannot silently make a menu non-deterministic.
                .order_by(TimeControlModel.display_order, TimeControlModel.id)
            )
        ).scalars()
        return tuple(_control_of(row) for row in rows)

    async def require(self, time_control_id: TimeControlId) -> TimeControl:
        """The control, or `UnsupportedTimeControl` — see the port.

        The predicate deliberately does **not** filter on `is_active`: the
        row is read either way, so the log line can say which of the two
        causes it was. The client is told neither.
        """
        row = await self._session.get(TimeControlModel, time_control_id)
        if row is None:
            logger.info(
                "time_control_unknown",
                extra={"time_control_id": time_control_id.value},
            )
            raise UnsupportedTimeControl("That time control is not available.")
        if not row.is_active:
            logger.info(
                "time_control_retired",
                extra={"time_control_id": time_control_id.value},
            )
            raise UnsupportedTimeControl("That time control is not available.")
        return _control_of(row)


def _control_of(row: TimeControlModel) -> TimeControl:
    """One row as the published value.

    A free function rather than a method on the model, because a model that
    knew how to become a domain value would be an ORM class the domain layer
    depends on — repositories.md §3's whole point.
    """
    return TimeControl(
        snapshot=TimeControlSnapshot(
            id=row.id,
            base_time_ms=row.base_time_ms,
            increment_ms=row.increment_ms,
            speed_class=row.speed_class,
        ),
        label=row.label,
        display_order=row.display_order,
        is_active=row.is_active,
    )


__all__ = ["SqlAlchemyTimeControlCatalogue"]
