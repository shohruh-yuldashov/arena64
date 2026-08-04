"""`game`'s adapters — the only layer here that knows PostgreSQL,
SQLAlchemy or a session exists.

    models.py       the `game` schema and `match`
    repositories/   `SqlAlchemyMatchRecordRepository`

Everything satisfies a port declared in `application/` (AD-06), so a use
case names a contract and never one of these classes.
"""

from app.modules.game.infrastructure.clock_deadline_store import RedisClockDeadlineStore
from app.modules.game.infrastructure.clock_tasks import (
    CLOCK_ADJUDICATION_TASK,
    ClockAdjudicationTask,
    adjudication_request,
)
from app.modules.game.infrastructure.live_match_store import RedisLiveMatchStore
from app.modules.game.infrastructure.models import GAME_SCHEMA, MatchRecordModel, MoveLogModel
from app.modules.game.infrastructure.repositories import (
    SqlAlchemyMatchRecordRepository,
    SqlAlchemyMatchRetentionStore,
)

__all__ = [
    "adjudication_request",
    "CLOCK_ADJUDICATION_TASK",
    "ClockAdjudicationTask",
    "RedisClockDeadlineStore",
    "MoveLogModel",
    "RedisLiveMatchStore",
    "GAME_SCHEMA",
    "MatchRecordModel",
    "SqlAlchemyMatchRecordRepository",
    "SqlAlchemyMatchRetentionStore",
]
