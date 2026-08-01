"""Adapters for the presence ports `users` publishes — A64-012.7.

The only place in `users` that knows Redis exists. Everything else in this
module's infrastructure layer speaks SQLAlchemy, and the split is the point:
domain-model.md §299 puts `Presence` in this module and in Redis, and those
are two independent decisions (DM-04).
"""

from app.modules.users.infrastructure.presence.keys import KEY_VERSION, presence_key
from app.modules.users.infrastructure.presence.presence_providers import (
    NoPresenceProvider,
    RedisPresenceProvider,
)

__all__ = [
    "KEY_VERSION",
    "NoPresenceProvider",
    "RedisPresenceProvider",
    "presence_key",
]
