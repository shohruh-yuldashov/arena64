"""`SessionScopedNotificationHandler` — the dispatcher, given a session.

The relay's `EventHandler` is long-lived: it is constructed once when the
worker starts and handed a batch on every tick. `SocialNotificationDispatcher`
is not — it holds repositories, repositories hold a session, and a session
must not outlive the unit of work it serves.

This class is the join. It implements `EventHandler`, and on every `handle`
it opens one session, builds the real dispatcher over it, delegates, and
closes. Exactly the lifetime a request gives a service, for a caller that is
not a request.

## Why not simply reuse the relay's session

The relay opens one per tick and could pass it down. It deliberately does
not, and the reason is transactional: the relay's session is the one that
holds the *claim* and writes the ledger, and its transactions are committed
around the handler call. A handler reading through the same session would
have its reads interleaved with the relay's commits — so "re-read the block
list at delivery" would silently mean "re-read it inside whatever
transaction state the relay happens to be in".

A separate session makes the consumer's reads plainly its own, which is what
"re-read current relationship state" has to mean if it is to mean anything.

## Where the wiring lives

Everything below is a call into the two modules' composition roots
(`profiles`' `build_profile_renderer`, `notifications`'
`build_social_notification_dispatcher`) rather than a graph assembled here.
That is what keeps the worker's object graph identical to the API's — see
`build_profile_renderer` on why a hand-rolled second composer would drift
into publishing fields the API withholds.
"""

import logging
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.clock import Clock
from app.database.redis import RedisPools
from app.modules.friends.application.ports import SocialGraphCache
from app.modules.friends.infrastructure.cache import (
    NoSocialGraphCache,
    RedisSocialGraphCache,
)
from app.modules.notifications.application.ports import NotificationSink
from app.modules.notifications.application.services import (
    CONSUMER_NAME,
    SUBSCRIBED_EVENT_TYPES,
)
from app.modules.notifications.presentation.dependencies import (
    build_social_notification_dispatcher,
)
from app.modules.profiles.presentation.dependencies import build_profile_renderer
from app.platform.outbox import EventFailure, OutboxEntry

logger = logging.getLogger(__name__)


class SessionScopedNotificationHandler:
    """`EventHandler` that scopes `SocialNotificationDispatcher` to a batch.

    Holds only long-lived things: a session factory, the Redis pools, the
    settings, a clock and the sink. Nothing per-request and nothing per-batch
    survives a call to `handle`.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        pools: RedisPools,
        settings: Settings,
        clock: Clock,
        sink: NotificationSink,
    ) -> None:
        self._session_factory = session_factory
        self._pools = pools
        self._settings = settings
        self._clock = clock
        self._sink = sink

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        # The dispatcher's own subscription set rather than a second list,
        # and answered without a session: the relay asks this per entry, and
        # building a dispatcher to evaluate a set-membership test would open
        # a connection for every event the consumer does not want.
        return event_type in SUBSCRIBED_EVENT_TYPES

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[EventFailure]:
        """One session, one dispatcher, one batch."""
        cache = self._social_graph_cache()
        async with self._session_factory() as session:
            dispatcher = build_social_notification_dispatcher(
                session,
                cache=cache,
                profiles=build_profile_renderer(
                    session,
                    pools=self._pools,
                    settings=self._settings,
                    # The **same** cache instance the dispatcher's own graph
                    # reader gets, so a block set read while resolving an
                    # audience and one read while rendering are one read.
                    cache=cache,
                    clock=self._clock,
                ),
                sink=self._sink,
            )
            return await dispatcher.handle(entries)

    def _social_graph_cache(self) -> SocialGraphCache:
        """The `friends:v1:` cache, or the inert stand-in.

        Built per call rather than held, because `RedisSocialGraphCache` is
        two attribute assignments over a process-lifetime pool — and because
        holding one would mean this class caring about a switch that
        `FRIENDS_CACHE_ENABLED` is allowed to flip between ticks.
        """
        if not self._settings.friends.cache_enabled:
            return NoSocialGraphCache()
        return RedisSocialGraphCache(self._pools.cache, settings=self._settings.friends)
