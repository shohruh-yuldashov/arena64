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

    AsyncSession                        one per **task run**, not a request
      -> SqlAlchemyQueueRepository
      -> PairingEngine                   pure, from RatingWindowPolicy
      -> PairingExclusionService         `friends`' BL-2 read
      -> GameRecentOpponents             `game`'s QT-3 read (A64-015.4)
      -> PersistentMatchCreation         `game`'s command port
      -> OutboxEventPublisher / SessionUnitOfWork
      -> PairingService

    AsyncSession                        one per **task run**, not a request
      -> SqlAlchemyQueueRepository
      -> GamePairingSettlements          did this ticket get a match
      -> MatchAcceptanceService          `game`'s overdue-match sweep
      -> OutboxEventPublisher / SessionUnitOfWork
      -> PairingReconciliationService

    AsyncSession                        one per request
      -> SqlAlchemyMatchRecordRepository
      -> MatchAcceptanceService          accept, decline, read your own
      -> PublicProfileService            the opponent preview, one id

Five factories, because there are five services and they differ in
capability rather than in wiring — see `application/services/__init__.py`.
`build_pairing_service` and `build_reconciliation_service` have **no
`Depends` wrapper**: nothing HTTP calls a pairing scan or a recovery pass,
so a route-layer accessor for either would be an entry point nobody should
have.

## `game`'s concrete classes are named here, and that is the pattern

A64-015.4 wires four `game` classes — a repository and three services —
from this file. That is the same arrangement `build_pairing_exclusions`
already uses for `friends`' `SqlAlchemyBlockedPlayerRepository`, and it is
why `.importlinter`'s privacy contracts take each module's `domain`,
`application` and `infrastructure` as sources and leave
`presentation/dependencies` outside them: a *service* that imported another
module's repository would be caught, and a composition root that constructs
one is what a root is for (BR-6).

Everything above the root still holds only ports. `PairingService` has a
`MatchCreationUseCase` and a `RecentOpponentProvider`; the acceptance route
has a `MatchAcceptanceUseCase`; the reconciler has two published reads. No
service on either side of the boundary can name a class from the other.

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
from app.modules.friends.application.services import PairingExclusionService
from app.modules.friends.infrastructure.repositories import SqlAlchemyBlockedPlayerRepository
from app.modules.friends.public import PairingExclusions
from app.modules.game.application.services import (
    GamePairingSettlements,
    GameRecentOpponents,
    MatchAcceptanceService,
    PersistentMatchCreation,
)
from app.modules.game.infrastructure.repositories import SqlAlchemyMatchRecordRepository
from app.modules.game.public import (
    GameEngineServices,
    MatchAcceptanceExpiryUseCase,
    MatchAcceptanceUseCase,
    MatchCreationUseCase,
    PairingReconciliationReader,
    engine_services,
)
from app.modules.matchmaking.application.eligibility import (
    PresenceEligibilityPolicy,
    QueueEligibilityPolicy,
)
from app.modules.matchmaking.application.ports import RecentOpponentProvider
from app.modules.matchmaking.application.services import (
    PairingReconciliationService,
    PairingService,
    QueueService,
)
from app.modules.matchmaking.domain.pairing import PairingEngine, RatingWindowPolicy
from app.modules.matchmaking.infrastructure import (
    ProvisionalRatingProvider,
    SqlAlchemyQueueRepository,
)
from app.modules.users.application.services.public_profile_service import PublicProfileService
from app.modules.users.application.services.user_service import UserService
from app.modules.users.infrastructure.presence import NoPresenceProvider, RedisPresenceProvider
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from app.modules.users.public import PresenceProvider, PublicProfileReader
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


def build_rating_window(settings: MatchmakingSettings) -> RatingWindowPolicy:
    """QT-5's widening window, from configuration — A64-015.3.

    Built from settings rather than hard-coded so a thin pool is widened by
    an operator rather than by a deploy, and so a test states a policy in
    one line instead of moving a clock four minutes.
    """
    return RatingWindowPolicy(
        initial_points=settings.rating_window_initial,
        widen_every_seconds=settings.rating_window_widen_every_seconds,
        widen_by_points=settings.rating_window_widen_by,
        maximum_points=settings.rating_window_maximum,
    )


def build_pairing_exclusions(session: AsyncSession) -> PairingExclusions:
    """`friends`' BL-2 read, over this run's session.

    Named concretely here — `SqlAlchemyBlockedPlayerRepository`,
    `PairingExclusionService` — which is what a composition root is for. The
    *service* holds only `friends.public.PairingExclusions` and so cannot
    reach anything else in that module.
    """
    return PairingExclusionService(SqlAlchemyBlockedPlayerRepository(session))


def build_match_creation(
    session: AsyncSession, *, events: EventPublisher, clock: Clock
) -> MatchCreationUseCase:
    """`game`'s side of the pairing handshake — A64-015.4.

    Real persistence, over the **same session** as the pairing scan's own
    repository. That is not a shortcut: the match's insert and its
    `game.match_created` outbox row must share one transaction (AD-16), and
    a second session would put them in two.

    It is emphatically *not* the same transaction as the ticket
    reservation, and cannot be — `PairingService` commits the claim before
    it calls this, because services.md BE-05 forbids holding two row locks
    across another module's work. The window that opens between them is
    what `PairingReconciliationService` exists to close.

    Replaces `UnavailableMatchCreation`, which A64-015.3 shipped because
    `game` had no table. It predicted this would be one line; it was.
    """
    return PersistentMatchCreation(
        matches=SqlAlchemyMatchRecordRepository(session),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_recent_opponents(session: AsyncSession) -> RecentOpponentProvider:
    """QT-3's rematch guard, read through `game.public` — A64-015.4 §11.

    `GameRecentOpponents` satisfies `matchmaking`'s own
    `RecentOpponentProvider` structurally, so there is no adapter between
    the two ports — see `application/ports.py` on why the shapes match
    deliberately.

    No unit of work and no publisher: it is a read, and a read that could
    write would be a capability nothing needs.
    """
    return GameRecentOpponents(SqlAlchemyMatchRecordRepository(session))


def build_pairing_settlements(session: AsyncSession) -> PairingReconciliationReader:
    """The reconciler's "did this ticket get a match" read — A64-015.4 §9."""
    return GamePairingSettlements(SqlAlchemyMatchRecordRepository(session))


def build_match_acceptance(
    session: AsyncSession, *, events: EventPublisher, clock: Clock
) -> MatchAcceptanceService:
    """`game`'s acceptance handshake, over one session — A64-015.4 §6.

    One object satisfying two published ports —
    `MatchAcceptanceUseCase` for the three routes and
    `MatchAcceptanceExpiryUseCase` for the reconciler — because they are
    two capabilities of one aggregate's lifecycle and splitting the
    *implementation* would mean two objects racing for the same rows. The
    **consumers** still hold one port each, which is where the split that
    matters is: a route cannot expire anybody's match.
    """
    return MatchAcceptanceService(
        matches=SqlAlchemyMatchRecordRepository(session),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_opponent_directory(session: AsyncSession, *, clock: Clock) -> PublicProfileReader:
    """How a pending match resolves its opponent to a handle.

    `users`' concrete classes, named here for the reason
    `build_pairing_exclusions` names `friends`': assembling another
    module's graph is what a composition root is for. The route holds
    `PublicProfileReader`, which has no way to read an email — see that
    port on why the leak is unreachable rather than merely avoided.
    """
    return PublicProfileService(
        UserService(
            users=SqlAlchemyUserRepository(session),
            unit_of_work=SessionUnitOfWork(session),
            clock=clock,
        )
    )


def build_pairing_service(
    session: AsyncSession,
    *,
    exclusions: PairingExclusions,
    opponents: RecentOpponentProvider,
    matches: MatchCreationUseCase,
    events: EventPublisher,
    settings: MatchmakingSettings,
    clock: Clock,
) -> PairingService:
    """One pairing scan's object graph, over one session.

    Plain arguments rather than `Depends`, for the reason
    `build_queue_service` takes them: the only caller is a background task,
    which has no request to resolve against. The difference is that this one
    has *no* `Depends` wrapper at all — a route that could trigger a pairing
    scan is an entry point nothing on this platform should have.

    The engine is constructed per call and that is deliberate rather than an
    oversight: it is a pure object over a frozen policy, so it costs nothing,
    and hoisting it to the process would make an operator's settings change
    require a restart for no benefit.
    """
    return PairingService(
        tickets=SqlAlchemyQueueRepository(session),
        engine=PairingEngine(build_rating_window(settings)),
        exclusions=exclusions,
        opponents=opponents,
        matches=matches,
        # Built over the **same** session as the repository, which is what
        # puts `PlayersPaired` in the transaction that marks both tickets
        # matched (AD-16).
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        candidate_batch_size=settings.candidate_batch_size,
        reservation_ttl_seconds=settings.reservation_ttl_seconds,
    )


def build_reconciliation_service(
    session: AsyncSession,
    *,
    settlements: PairingReconciliationReader,
    acceptance: MatchAcceptanceExpiryUseCase,
    events: EventPublisher,
    settings: MatchmakingSettings,
    clock: Clock,
) -> PairingReconciliationService:
    """One reconciliation pass's object graph, over one session — §9.

    Plain arguments and **no `Depends` wrapper**, exactly like
    `build_pairing_service`: the only caller is a background task, and a
    route that could trigger a recovery pass is an entry point nothing on
    this platform should have. A64-015.4 §9 is explicit that the manual
    repair path must not be the primary mechanism, and the way to make that
    structural is to give the manual path no door.

    Both `game` reads are built over the **same session**, so the sweep
    that expires overdue matches and the writes that settle their tickets
    are one connection's work rather than two — which is what lets the
    outbox rows for both land in one transaction each.
    """
    return PairingReconciliationService(
        tickets=SqlAlchemyQueueRepository(session),
        settlements=settlements,
        acceptance=acceptance,
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        batch_size=settings.reconciliation_batch_size,
    )


def get_match_acceptance(
    session: DbSessionDep, clock: ClockDep, events: EventPublisherDep
) -> MatchAcceptanceUseCase:
    """The per-request acceptance use case.

    Typed as the **port**, never as `MatchAcceptanceService` — so a route
    annotating this dependency holds three methods and cannot reach
    `expire_overdue` even by accident, though the object in its hand has
    it.
    """
    return build_match_acceptance(session, events=events, clock=clock)


MatchAcceptanceDep = Annotated[MatchAcceptanceUseCase, Depends(get_match_acceptance)]


def get_opponent_directory(session: DbSessionDep, clock: ClockDep) -> PublicProfileReader:
    """The per-request opponent lookup."""
    return build_opponent_directory(session, clock=clock)


OpponentDirectoryDep = Annotated[PublicProfileReader, Depends(get_opponent_directory)]


__all__ = [
    "EligibilityPolicyDep",
    "EngineServicesDep",
    "MatchAcceptanceDep",
    "OpponentDirectoryDep",
    "PresenceReaderDep",
    "QueueServiceDep",
    "build_eligibility_policy",
    "build_match_acceptance",
    "build_match_creation",
    "build_opponent_directory",
    "build_pairing_exclusions",
    "build_pairing_service",
    "build_pairing_settlements",
    "build_queue_service",
    "build_rating_window",
    "build_recent_opponents",
    "build_reconciliation_service",
    "get_eligibility_policy",
    "get_engine_services",
    "get_match_acceptance",
    "get_opponent_directory",
    "get_presence_reader",
    "get_queue_service",
]
