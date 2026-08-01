"""The `friends:v1:` cache — A64-013.6.

The first Redis cache on the platform, and the only one whose invalidation
rules were complete enough to write (caching.md C-1).
"""

from app.modules.friends.infrastructure.cache.keys import (
    KEY_VERSION,
    key_for,
    keys_for,
)
from app.modules.friends.infrastructure.cache.social_graph_cache import (
    NoSocialGraphCache,
    RedisSocialGraphCache,
)

__all__ = [
    "KEY_VERSION",
    "NoSocialGraphCache",
    "RedisSocialGraphCache",
    "key_for",
    "keys_for",
]
