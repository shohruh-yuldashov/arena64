"""Database health checks — extracted from the health *endpoint*
(`app/api/v1/health.py`) so "is the database reachable" is a plain,
testable function independent of HTTP. A future Celery task or the clock
loop (`services.md` BE-01 — neither has an HTTP route) can call the same
check without importing FastAPI.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def check_database_connection(session: AsyncSession) -> bool:
    """Never raises — a readiness probe that raises defeats its own
    purpose. CLAUDE.md §9 rule 8: a down dependency is an *expected*
    outcome for a readiness check, not a defect to propagate.
    """
    try:
        await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — a readiness probe must not raise
        logger.warning("database_health_check_failed", exc_info=True)
        return False
    return True
