"""`GameAbandonedMatchRetention` — `game`'s side of A64-015.5 §8.

Implements `game.public.AbandonedMatchRetention`, whose docstring records
why the horizon is supplied by `matchmaking` and why the capability is
published as a command rather than as a schedule of its own.

Four lines of body over a store, and that is the point of the port: the
*work* is one bounded delete, and what makes it worth publishing is that
`matchmaking` cannot perform it without importing a `game` table.

## The alarm is the interesting return value

`unsettled_before` is not used to decide anything and is reported anyway. A
match still `pending_acceptance` past the whole retention horizon means the
acceptance-expiry sweep has stopped: two players are holding an offer with a
deadline that passed days ago, `MATCHMAKING_RECONCILIATION_ENABLED` is off
somewhere it should not be, and nothing else on the platform would say so.
"""

import logging
from datetime import datetime

from app.core.unit_of_work import UnitOfWork
from app.modules.game.application.ports import MatchRetentionStore

logger = logging.getLogger(__name__)


class GameAbandonedMatchRetention:
    """The abandoned-match sweep, over one session."""

    def __init__(self, *, store: MatchRetentionStore, unit_of_work: UnitOfWork) -> None:
        self._store = store
        self._unit_of_work = unit_of_work

    async def prune_abandoned(self, *, before: datetime, batch_size: int) -> int:
        """Deletes one bounded batch of cancelled or expired matches.

        **One transaction per call**, not per run: the caller drains in
        batches and each commits on its own, so the locks a batch took are
        released before the next is taken. Holding them across twenty
        batches would reproduce the unbounded `DELETE` this design exists
        to avoid.

        Propagates rather than swallowing. The caller is a retention job
        that already records its own failures and never raises; a second
        swallow here would leave it reporting a successful run that deleted
        nothing.
        """
        async with self._unit_of_work:
            deleted = await self._store.prune_abandoned(before=before, batch_size=batch_size)
            await self._unit_of_work.commit()

        if deleted:
            logger.debug("abandoned_matches_pruned", extra={"deleted": deleted})
        return deleted

    async def unsettled_before(self, instant: datetime) -> int:
        """How many matches older than `instant` are still awaiting an
        answer — see this module's docstring on why that is an alarm."""
        async with self._unit_of_work:
            pending = await self._store.unsettled_before(instant)
            await self._unit_of_work.commit()
        return pending


__all__ = ["GameAbandonedMatchRetention"]
