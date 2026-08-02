"""`game`'s adapters — the only layer here that knows PostgreSQL,
SQLAlchemy or a session exists.

    models.py       the `game` schema and `match`
    repositories/   `SqlAlchemyMatchRecordRepository`

Everything satisfies a port declared in `application/` (AD-06), so a use
case names a contract and never one of these classes.
"""

from app.modules.game.infrastructure.models import GAME_SCHEMA, MatchRecordModel
from app.modules.game.infrastructure.repositories import SqlAlchemyMatchRecordRepository

__all__ = ["GAME_SCHEMA", "MatchRecordModel", "SqlAlchemyMatchRecordRepository"]
