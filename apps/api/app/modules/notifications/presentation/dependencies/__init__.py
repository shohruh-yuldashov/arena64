"""The `Depends` bridge for `notifications` — DI-01.

**One factory with an HTTP caller and one without**, which is the shape a
module built around an outbox has:

    get_presence_notification_service    resolved by `auth`'s three lifecycle
                                         routes. The producer
    build_social_notification_dispatcher not a dependency at all — the relay
                                         worker builds one per tick over its
                                         own session, because there is no
                                         request to scope it to

The second is a plain function rather than a `Depends` for exactly that
reason. A `Depends`-shaped factory that nothing resolves through FastAPI
would be a misleading signature, and a caller invoking `get_x(session=...)`
by hand is how a dependency stops being one.

## Why the producer is here and not in `users`

`PresenceService` lives in `users`, which owns `Presence`. What A64-013.7
adds is *transition detection plus event emission*, which is neither a
presence concern nor a friends concern — it is the notification pipeline's
entry point, and it holds an `EventPublisher` that `users` has no other
reason to know about.

`auth` resolving a `notifications` dependency is the same arrangement it
already has with `users`, and `profiles` with `avatars`: a module's
presentation layer reaching another module's published capability at the
composition root.
"""

import logging
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ClockDep, DbSessionDep, PresenceSettingsDep
from app.api.outbox_deps import EventPublisherDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.application.ports import SocialGraphCache
from app.modules.friends.application.services import PresenceAudienceService
from app.modules.friends.application.services.cached_social_graph_reader import (
    CachedSocialGraphReader,
)
from app.modules.friends.application.services.social_graph_reader import (
    SocialGraphReaderService,
)
from app.modules.friends.infrastructure.repositories import (
    SqlAlchemyBlockedPlayerRepository,
    SqlAlchemyFriendshipRepository,
)
from app.modules.notifications.application.ports import NotificationSink
from app.modules.notifications.application.services import (
    PresenceNotificationService,
    SocialNotificationDispatcher,
)
from app.modules.profiles.application.services.profile_renderer import BatchProfileRenderer
from app.modules.users.presentation.dependencies import PresenceServiceDep
from app.platform.outbox import NoEventPublisher

logger = logging.getLogger(__name__)


def get_presence_notification_service(
    presence: PresenceServiceDep,
    events: EventPublisherDep,
    presence_settings: PresenceSettingsDep,
    session: DbSessionDep,
    clock: ClockDep,
) -> PresenceNotificationService:
    """The presence **producer** — what `auth`'s routes hold.

    `PresenceService` is injected as `PresenceWriter`, the narrower shape
    this module declares: the composition root knows the concrete service
    satisfies it, and the service that holds it cannot do anything with
    presence beyond read-then-write one player's record.

    The unit of work is built over the request's session, so the outbox row
    commits on the connection the request already has. It is still its own
    transaction — see `PresenceNotificationService` on why presence is the
    one producer that cannot give AD-16's guarantee, and what is done
    instead.

    ## `PRESENCE_ENABLED=false` disables presence *events* too

    `NoPresenceProvider` accepts writes and discards them, so every read
    afterwards reports nobody as present. Transition detection compares the
    previous record against the new one — and against a store that never
    remembers anything, **every** sign-in and every token refresh looks like
    an `offline -> online` edge.

    The result would be an event per refresh, per player, forever: exactly
    the flood the transition check exists to prevent, produced by the switch
    that was supposed to make presence cost nothing. So the kill switch
    reaches the publisher too, and nothing else changes — `auth`'s routes
    hold the same service and call the same methods.

    Only the *presence* half is switched off. Friend, block and unblock
    events still publish through the real one: they read their state from
    PostgreSQL, which is not what this flag turns off.
    """
    if not presence_settings.enabled:
        logger.warning("presence_events_disabled", extra={"reason": "presence_disabled"})
        return PresenceNotificationService(
            presence=presence,
            events=NoEventPublisher(),
            unit_of_work=SessionUnitOfWork(session),
            clock=clock,
        )

    return PresenceNotificationService(
        presence=presence,
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


PresenceNotificationServiceDep = Annotated[
    PresenceNotificationService, Depends(get_presence_notification_service)
]


def build_social_notification_dispatcher(
    session: AsyncSession,
    *,
    cache: SocialGraphCache,
    profiles: BatchProfileRenderer,
    sink: NotificationSink,
) -> SocialNotificationDispatcher:
    """The outbox consumer, assembled over a worker's session.

    Every collaborator is read-only, and that is the property worth stating:
    a consumer that ran minutes after the fact and could *write* to the
    social graph would be able to act on state it read at enqueue time. It
    reads relationships, reads profiles, and delivers.

    The social graph arrives through `CachedSocialGraphReader`, the same
    decorator the HTTP path uses, so the relay's block re-check is served
    from `friends:v1:` on a hot backlog rather than querying per event —
    and, more importantly, so the worker and the API can never disagree
    about what the graph says.
    """
    graph = CachedSocialGraphReader(
        SocialGraphReaderService(
            friendships=SqlAlchemyFriendshipRepository(session),
            blocks=SqlAlchemyBlockedPlayerRepository(session),
        ),
        cache,
    )
    return SocialNotificationDispatcher(
        audience=PresenceAudienceService(
            friendships=SqlAlchemyFriendshipRepository(session),
            blocks=SqlAlchemyBlockedPlayerRepository(session),
        ),
        graph=graph,
        profiles=profiles,
        sink=sink,
    )


__all__ = [
    "PresenceNotificationServiceDep",
    "build_social_notification_dispatcher",
    "get_presence_notification_service",
]
