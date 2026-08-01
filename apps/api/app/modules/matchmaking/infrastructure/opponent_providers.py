"""`NoRecentOpponents` — the rematch guard, until there is a history to
guard against.

A64-015.3 §6 asks for the port, a safe no-op, and the deferred integration
recorded. This is the no-op, and the record is here.

## Why this is a real class and not a `None` the service checks for

`PairingService` holds a `RecentOpponentProvider` unconditionally, so there
is no branch anywhere for "history is not available yet". The day
`game.public` publishes the read, the composition root swaps this object
and nothing else in the graph changes — no `if`, no flag, no test.

A `None` collaborator would have put that branch in the service, where it
would have outlived the reason for it.

## What it costs to be wrong in this direction

It excludes nothing, so two players who just finished a game may be paired
again immediately. That is a disappointment. The other direction —
guessing at a history this module cannot read, or refusing to pair while
it is unavailable — is an empty pool, which is an outage. The safe
direction is the one that keeps players playing.
"""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

logger = logging.getLogger(__name__)


class NoRecentOpponents:
    """Excludes nobody. See this module's docstring."""

    async def recent_opponents_among(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, frozenset[UUID]]:
        return {}


__all__ = ["NoRecentOpponents"]
