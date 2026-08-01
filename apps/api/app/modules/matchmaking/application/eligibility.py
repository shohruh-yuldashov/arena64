"""May this player join a queue? — A64-015.2.

A64-015.1 asked one question at entry and asked it inline: is the player
positively recorded as offline. That was the only check any module could
answer, and putting it in `QueueService.join` was right for one check.

There will be more, and they come from modules that mostly do not exist:

| Check | Owner | Status |
| --- | --- | --- |
| Positively recorded offline | `users` (presence) | **Implemented** |
| Account suspended or closed | `auth` | No published port |
| Active sanction | `admin` | Module does not exist |
| Already in a live match | `game` | No match exists to be in |
| Region locked out | `admin` | Not a rule anybody has written |

A service that grew an `if` per module would end up holding five ports and
answering a question none of them is about. So the question becomes a port
of its own — one `require_eligible` — and the service holds that instead.

## Why the refusal says so little

`QueueNotPermitted` carries one message for every cause, and the message
names none of them. That is not laziness; it is the same rule
`FriendRequestRecipientUnavailable` follows for the same reason.

The checks this port will grow include **block relationships** (BL-2 makes
a blocked pair unpairable) and **sanctions**. A refusal that said "you may
not queue because you are sanctioned" is arguably fine; one that varied by
*who else is in the pool* would let a player probe the block graph by
queueing repeatedly, which is precisely what BL-1's "the blocked player is
never told" exists to prevent. Since the port cannot distinguish those
futures at the call site, it says one thing now and keeps saying it.

Block filtering itself is **not** here and is not an eligibility question:
it is a pairing-time exclusion between two specific players (QT-3), and it
belongs to the scan that has both of them. A64-015.3.
"""

import logging
from typing import Protocol
from uuid import UUID

from app.modules.matchmaking.domain.exceptions import QueueNotPermitted
from app.modules.matchmaking.domain.queue_pool import QueuePool
from app.modules.users.public import PresenceProvider

logger = logging.getLogger(__name__)


class QueueEligibilityPolicy(Protocol):
    """Whether a player may enter a pool at all.

    Distinct from QT-1's "one live ticket" and from pool validity: those
    are facts about the *request*, checked by the service and by
    `QueuePool` itself. This is a fact about the *player*.
    """

    async def require_eligible(self, player_id: UUID, *, pool: QueuePool) -> None:
        """Passes silently, or raises `QueueNotPermitted`.

        A command rather than a predicate returning a reason. A caller that
        received a reason would be tempted to render it, and §8's rule is
        that a player-facing failure must not reveal why — see the module
        docstring on the block graph.
        """
        ...


class PresenceEligibilityPolicy:
    """The one check this platform can currently make.

    Refuses a player the platform has **positively observed** signing out,
    and nobody else.

    `PresenceProvider.presence_for` collapses three situations into `None`
    — the window expired, nothing was ever recorded, Redis was unreachable
    — and its own docstring says a caller "must not try" to tell them
    apart. So `None` is permitted, and the only refusal is a record that
    says `online: false`.

    The asymmetry is deliberate and is the safe direction. Refusing on
    `None` would mean a Redis blip stopped everybody queueing — the
    self-inflicted outage system-design.md T-2 warns about — in exchange
    for excluding players the platform never observed at all.

    Holds a `PresenceProvider` and **not** a `PresenceRecorder`: this class
    must not be able to write presence, and the port it does not hold is
    what guarantees that rather than a convention.
    """

    def __init__(self, presence: PresenceProvider) -> None:
        self._presence = presence

    async def require_eligible(self, player_id: UUID, *, pool: QueuePool) -> None:
        record = await self._presence.presence_for(player_id)
        if record is not None and not record.is_online:
            logger.info(
                "queue_join_refused",
                extra={"player_id": str(player_id), "pool": pool.identifier(), "reason": "offline"},
            )
            raise QueueNotPermitted("You cannot join a queue right now.")


class AlwaysEligible:
    """Permits everybody.

    For a deployment with presence disabled, and for tests whose subject is
    something other than eligibility. It is a real implementation rather
    than a mock, so the composition root has something to wire when
    `PRESENCE_ENABLED` is off and no test has to invent one.
    """

    async def require_eligible(self, player_id: UUID, *, pool: QueuePool) -> None:
        return None


__all__ = ["AlwaysEligible", "PresenceEligibilityPolicy", "QueueEligibilityPolicy"]
