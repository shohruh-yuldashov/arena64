"""`SessionScopedNotificationHandler` — the dispatcher, given a session.

The relay's `EventHandler` is long-lived: it is constructed once when the
worker starts and handed a batch on every tick. `SocialNotificationDispatcher`
is not — it holds repositories, repositories hold a session, and a session
must not outlive the unit of work it serves.

This class is the join. It implements `EventHandler`, and on every `handle`
it opens one session, builds a dispatcher over it, delegates, and closes.
Exactly the lifetime a request gives a service, for a caller that is not a
request.

## It takes a factory rather than building the graph — A64-013.8

The first version of this class imported `friends`, `profiles` and its own
`presentation` package to assemble the dispatcher itself. Every one of those
was a real boundary violation, and the import contract added by A64-013.8 is
what found them: an `infrastructure` module reaching sideways into three
other modules and upwards into its own composition root.

The fix is an inversion rather than a rearrangement. This class now takes

    dispatcher_factory: (AsyncSession) -> EventHandler

so what it knows is *session lifetime*, which is genuinely infrastructure,
and what it does not know is *which objects* — which is genuinely the
composition root's (`app/app_factory.py`). The file's imports are now
SQLAlchemy, the outbox ports and nothing else.

## Why not simply reuse the relay's session

The relay opens one per tick and could pass it down. It deliberately does
not, and the reason is transactional: the relay's session is the one that
holds the *claim* and writes the ledger, and its transactions are committed
around the handler call. A handler reading through the same session would
have its reads interleaved with the relay's commits — so "re-read the block
list at delivery" would silently mean "re-read it inside whatever transaction
state the relay happens to be in".

A separate session makes the consumer's reads plainly its own, which is what
"re-read current relationship state" has to mean if it is to mean anything.
"""

import logging
from collections.abc import Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.platform.outbox import EventFailure, EventHandler, OutboxEntry

logger = logging.getLogger(__name__)

#: What the composition root supplies: a dispatcher over one session.
#:
#: Typed as `EventHandler` rather than as `SocialNotificationDispatcher` for
#: the same reason the class below takes a factory at all — this module must
#: not know which consumer it is scoping. Any batch-shaped consumer that needs
#: a session can be wrapped by this class unchanged.
DispatcherFactory = Callable[[AsyncSession], EventHandler]


class SessionScopedNotificationHandler:
    """Gives a session-bound consumer the lifetime a relay tick needs.

    Holds only long-lived things: a session factory, the consumer's name, and
    the subscription set. Nothing per-batch survives a call to `handle`.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher_factory: DispatcherFactory,
        consumer: str,
        event_types: frozenset[str],
    ) -> None:
        self._session_factory = session_factory
        self._dispatcher_factory = dispatcher_factory
        self._consumer = consumer
        self._event_types = event_types

    @property
    def consumer(self) -> str:
        return self._consumer

    def handles(self, event_type: str) -> bool:
        # Answered from a set, without opening a session: the relay asks this
        # per entry, and building a dispatcher to evaluate a membership test
        # would open a connection for every event the consumer does not want.
        return event_type in self._event_types

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[EventFailure]:
        """One session, one dispatcher, one batch."""
        async with self._session_factory() as session:
            return await self._dispatcher_factory(session).handle(entries)
