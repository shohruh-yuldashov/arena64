"""What other modules may reach — R-1.

One port today, and it is the one the platform will need before it needs
anything else here: **erasure**.

Arena64 has no account deletion implementation. When it gets one, the
transaction that erases an account calls `AnalyticsEraser.erase` inside
itself, and this is the surface it imports — not the service, not the
repository, and certainly not the model. Nothing else about analytics is
any other module's business: a domain module produces facts and analytics
consumes them, never the other way round.

There is deliberately **no read surface**. Raw analytics is not reachable
through any product API (§66), and A64-027.6's dashboard will read
aggregates rather than rows.
"""

from typing import Protocol
from uuid import UUID


class AnalyticsEraser(Protocol):
    """Destroys a player's link to their analytics history — D3.

    Idempotent, and returns whether a link existed. Erasing an account that
    never produced an analytics event is not an error: a deletion request
    that failed because there was nothing to delete would make the retry of
    a deletion fail.

    Call it **inside** the erasure transaction. The analytics rows survive
    and stop naming anybody; see `analytics/application/services/erasure.py`
    on why that is the erasure rather than a partial one.
    """

    async def erase(self, player_id: UUID) -> bool: ...


__all__ = ["AnalyticsEraser"]
