"""The FastAPI `Depends` bridge for `matchmaking` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request:

    AsyncSession                        one per request (`app.api.deps`)
      -> SqlAlchemyQueueRepository
      -> ProvisionalRatingProvider      until `rating` exists
      -> QueueEligibilityPolicy         presence-backed, or the permissive one
      -> OutboxEventPublisher           over the same session (AD-16)
      -> SessionUnitOfWork
      -> QueueService

    GameEngineServices                  one per **process**, not per request

One factory, because there is one service. A64-014.2 adds a second for
pairing rather than widening this one — see
`application/services/__init__.py` on why the split is the capability.

## The presence adapter is built here, not imported from `users`

`get_presence_reader` names `RedisPresenceProvider` and `NoPresenceProvider`
directly, which is exactly what a composition root is for (BR-6 forbids a
*module* reaching for the container; the root wiring modules together is the
root's job) — and it is why `.importlinter`'s privacy contracts take each
module's `domain`, `application` and `infrastructure` as sources and leave
`presentation/dependencies` outside them.

**`presence:v1:`, never a matchmaking-owned index.** A64-014.1 is explicit
("do not create another online-player index"), and caching.md C-8 gives the
general reason: a namespace has exactly one owner and one writer, because
two writers with different shapes is the failure a version segment cannot
fix. `matchmaking` is a *reader*, holds `PresenceProvider` rather than
`PresenceRecorder`, and therefore cannot structurally become a second
writer.

## Why `build_queue_service` takes plain arguments

It takes a session, a presence reader, a publisher and settings rather than
resolving `Depends`, so the expiry task — which has no request, no
`app.state` and no route — builds the identical graph from `app_factory`. A
factory reachable only through `Depends` would mean the background path
assembling its own copy, and the two drifting the first time either gained a
collaborator.
"""

import logging
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ClockDep, DbSessionDep, PresenceSettingsDep, RedisPoolsDep, SettingsDep
from app.api.outbox_deps import EventPublisherDep
from app.config.settings import MatchmakingSettings
from app.core.clock import Clock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.public import GameEngineServices, engine_services
from app.modules.matchmaking.application.eligibility import (
    PresenceEligibilityPolicy,
    QueueEligibilityPolicy,
)
from app.modules.matchmaking.application.services import QueueService
from app.modules.matchmaking.infrastructure import (
    ProvisionalRatingProvider,
    SqlAlchemyQueueRepository,
)
from app.modules.users.infrastructure.presence import NoPresenceProvider, RedisPresenceProvider
from app.modules.users.public import PresenceProvider
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


def get_presence_reader(
    pools: RedisPoolsDep, settings: PresenceSettingsDep, clock: ClockDep
) -> PresenceProvider:
    """The presence adapter this request reads through.

    The **`cache` Redis role**, because that is where presence lives
    (caching.md §3.2) — this resolves the same keys `profiles` reads, from
    the same instance, through the same adapter class.

    Branches on `PRESENCE_ENABLED` exactly as every other presence factory
    does, and the degradation is the correct direction: with presence off,
    `NoPresenceProvider` reports `None` for everybody and
    `QueueService.join` therefore refuses nobody. The check exists to
    exclude players the platform has *positively observed* signing out, and
    with presence off it has observed nothing.

    Typed as the **port**, never as `RedisPresenceProvider` — so a route or
    a service annotating this dependency cannot name a concrete adapter
    even by accident.
    """
    if not settings.enabled:
        return NoPresenceProvider()
    return RedisPresenceProvider(pools.cache, settings=settings, clock=clock)


PresenceReaderDep = Annotated[PresenceProvider, Depends(get_presence_reader)]


def build_eligibility_policy(presence: PresenceProvider) -> QueueEligibilityPolicy:
    """The checks a player must pass to enter a pool — A64-015.2.

    One implementation today, backed by the presence reader above. It is
    built here rather than inside `QueueService` for the reason every
    adapter is: the service depends on the port, and the root decides which
    adapter satisfies it.
    """
    return PresenceEligibilityPolicy(presence)


def get_eligibility_policy(presence: PresenceReaderDep) -> QueueEligibilityPolicy:
    return build_eligibility_policy(presence)


EligibilityPolicyDep = Annotated[QueueEligibilityPolicy, Depends(get_eligibility_policy)]


def get_engine_services() -> GameEngineServices:
    """The **process-wide** engine collaborators — A64-015.2.

    Every one is stateless, so one instance serves every request; `Depends`
    hands out the same object rather than building a graph per call.
    `specs/game-engine/audit.md` §14 asks for exactly this, and §3 of
    A64-015.2 forbids the alternative — a route handler or a queue service
    constructing its own.

    **Nothing consumes it yet**, and that is stated rather than hidden: this
    task creates no match, so no queue code calls the engine. It is wired
    now because the seam is much cheaper to establish than to retrofit once
    a pairing worker exists and has already made its own.
    """
    return engine_services()


EngineServicesDep = Annotated[GameEngineServices, Depends(get_engine_services)]


def build_queue_service(
    session: AsyncSession,
    *,
    eligibility: QueueEligibilityPolicy,
    events: EventPublisher,
    settings: MatchmakingSettings,
    clock: Clock,
) -> QueueService:
    """The queue use cases, assembled over one session.

    Called from the `Depends` factory below for a request, and from
    `app_factory` for the expiry task — see this module's docstring on why
    one function serves both.

    The `Clock` is injected rather than read (AD-07). `entered_at`,
    `expires_at` and every expiry decision come from it, so the whole
    ten-minute window is a unit test that runs in a microsecond rather than
    one that sleeps.
    """
    return QueueService(
        tickets=SqlAlchemyQueueRepository(session),
        # Until `rating` exists. See `ProvisionalRatingProvider` on why the
        # port is here rather than a constant inside the service.
        ratings=ProvisionalRatingProvider(),
        eligibility=eligibility,
        # Built over the **same** session as the repository, which is what
        # puts the outbox row in the ticket's transaction rather than beside
        # it (AD-16).
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        ticket_ttl_seconds=settings.ticket_ttl_seconds,
        snapshot_limit=settings.snapshot_limit,
    )


def get_queue_service(
    session: DbSessionDep,
    clock: ClockDep,
    events: EventPublisherDep,
    settings: SettingsDep,
    eligibility: EligibilityPolicyDep,
) -> QueueService:
    """The per-request `QueueService`."""
    return build_queue_service(
        session,
        eligibility=eligibility,
        events=events,
        settings=settings.matchmaking,
        clock=clock,
    )


QueueServiceDep = Annotated[QueueService, Depends(get_queue_service)]


__all__ = [
    "EligibilityPolicyDep",
    "EngineServicesDep",
    "PresenceReaderDep",
    "QueueServiceDep",
    "build_eligibility_policy",
    "build_queue_service",
    "get_eligibility_policy",
    "get_engine_services",
    "get_presence_reader",
    "get_queue_service",
]
