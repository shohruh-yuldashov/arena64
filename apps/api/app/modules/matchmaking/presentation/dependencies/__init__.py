"""The FastAPI `Depends` bridge for `matchmaking` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request:

    AsyncSession                        one per request (`app.api.deps`)
      -> SqlAlchemyQueueRepository
      -> PublishedRatingProvider        over `rating.public` (A64-017.2)
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
from app.database.unit_of_work import ParticipatingUnitOfWork, SessionUnitOfWork
from app.modules.friends.application.ports import SocialGraphCache
from app.modules.friends.application.services import (
    PairingExclusionService,
    SocialGraphReaderService,
)
from app.modules.friends.application.services.cached_social_graph_reader import (
    CachedSocialGraphReader,
)
from app.modules.friends.infrastructure.repositories import (
    SqlAlchemyBlockedPlayerRepository,
    SqlAlchemyFriendshipRepository,
)
from app.modules.friends.presentation.dependencies import SocialGraphCacheDep
from app.modules.friends.public import PairingExclusions
from app.modules.game.application.ports import ClockDeadlineStore
from app.modules.game.application.services import (
    GameAbandonedMatchRetention,
    GamePairingSettlements,
    GameRecentOpponents,
    MatchAcceptanceService,
    PersistentMatchCreation,
)
from app.modules.game.infrastructure.clock_deadline_store import RedisClockDeadlineStore
from app.modules.game.infrastructure.repositories import (
    SqlAlchemyMatchRecordRepository,
    SqlAlchemyMatchRetentionStore,
)
from app.modules.game.public import (
    GameEngineServices,
    MatchAcceptanceExpiryUseCase,
    MatchAcceptanceUseCase,
    MatchCreationUseCase,
    PairingReconciliationReader,
    engine_services,
)
from app.modules.matchmaking.application.eligibility import (
    AllEligibilityChecks,
    CooldownEligibilityPolicy,
    PresenceEligibilityPolicy,
    QueueEligibilityPolicy,
)
from app.modules.matchmaking.application.ports import (
    CooldownAuditRepository,
    CooldownRepository,
    PendingMatchSink,
    RecentOpponentProvider,
    ReconciliationTimelineRepository,
)
from app.modules.matchmaking.application.services import (
    MatchOutcomeService,
    PairingReconciliationService,
    PairingService,
    PendingMatchNotifier,
    QueueRetentionService,
    QueueService,
    ReconciliationTimelineProjector,
    queue_retention_policy,
)
from app.modules.matchmaking.application.services.challenge_expiry_service import (
    ChallengeExpiryService,
)
from app.modules.matchmaking.application.services.challenge_service import ChallengeService
from app.modules.matchmaking.domain.pairing import PairingEngine, RatingWindowPolicy
from app.modules.matchmaking.infrastructure import (
    PublishedRatingProvider,
    SqlAlchemyCooldownAuditRepository,
    SqlAlchemyCooldownRepository,
    SqlAlchemyQueueRepository,
    SqlAlchemyQueueRetentionStore,
    SqlAlchemyReconciliationTimelineRepository,
)
from app.modules.matchmaking.infrastructure.repositories.challenge_repository import (
    SqlAlchemyChallengeRepository,
)
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyRatingReader,
)
from app.modules.reference.infrastructure.repositories import SqlAlchemyTimeControlCatalogue
from app.modules.users.application.services.public_profile_service import PublicProfileService
from app.modules.users.application.services.user_service import UserService
from app.modules.users.infrastructure.presence import NoPresenceProvider, RedisPresenceProvider
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from app.modules.users.public import PresenceProvider, PublicProfileReader
from app.platform.metrics import MetricsRecorder, process_metrics
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


def build_eligibility_policy(
    session: AsyncSession, presence: PresenceProvider, *, clock: Clock
) -> QueueEligibilityPolicy:
    """The checks a player must pass to enter a pool — A64-015.2,
    A64-015.5 §3.

    Two implementations now, composed into one port by
    `AllEligibilityChecks`. `QueueService` still holds a single
    `QueueEligibilityPolicy`, which is what A64-015.2 predicted the port
    would buy: "a service that grew an `if` per module would end up holding
    five ports and answering a question none of them is about."

    Order is significant — presence first, cooldown second — so a player who
    fails both is refused by the check that says less. See
    `AllEligibilityChecks`.
    """
    return AllEligibilityChecks(
        [
            PresenceEligibilityPolicy(presence),
            CooldownEligibilityPolicy(build_cooldowns(session), clock=clock),
        ]
    )


def get_eligibility_policy(
    session: DbSessionDep, presence: PresenceReaderDep, clock: ClockDep
) -> QueueEligibilityPolicy:
    return build_eligibility_policy(session, presence, clock=clock)


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
        # `rating.public`'s reader since A64-017.2 — the constant this
        # used to be is gone. See `PublishedRatingProvider` on why the
        # translation lives in an adapter rather than in the service.
        ratings=PublishedRatingProvider(SqlAlchemyRatingReader(session)),
        # `reference.public`'s catalogue — A64-020.5A-pre. Constructed here
        # rather than declared as a local port, for the reason presence is:
        # `reference` already publishes the contract, and a second one would
        # be two definitions of what a time control is.
        #
        # Over the **same** session as the repository. That is not required
        # for correctness — the read happens before the ticket's transaction
        # opens (BE-05) — and it is what stops a join holding two
        # connections for one request.
        time_controls=SqlAlchemyTimeControlCatalogue(session),
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
    session: AsyncSession,
    *,
    events: EventPublisher,
    clock: Clock,
    metrics: MetricsRecorder,
    deadlines: ClockDeadlineStore,
) -> MatchAcceptanceService:
    """`game`'s acceptance handshake, over one session — A64-015.4 §6,
    A64-015.5 §10.

    One object satisfying two published ports —
    `MatchAcceptanceUseCase` for the three routes and
    `MatchAcceptanceExpiryUseCase` for the reconciler — because they are
    two capabilities of one aggregate's lifecycle and splitting the
    *implementation* would mean two objects racing for the same rows. The
    **consumers** still hold one port each, which is where the split that
    matters is: a route cannot expire anybody's match.

    ## The one factory, and the four callers that share it

    A64-015.5 §10 asks that the route path and the reconciliation task not
    build this independently. They already did not — this function has been
    the single construction site since A64-015.4 — and §10's real value is
    that a **third and fourth** caller arrived without duplicating anything:

        get_match_acceptance          the three HTTP routes
        _reconciliation_service_for   the recovery task's expiry sweep
        _pending_match_notifier_for   the realtime consumer's re-read (§4)
        (a fifth needs no new code)

    What is hoisted is the **factory, not the service**. A single shared
    instance is exactly what this must not be: it holds a repository, the
    repository holds a session, and a session must not outlive the unit of
    work it serves. Each caller gets its own graph over its own session, and
    what they share is the definition — so a collaborator added here reaches
    all four at once.

    The one genuinely shared object is the metrics recorder, which is
    stateless and process-wide (`get_metrics`).

    ## The deadline store, and why it is a required argument

    A64-020.5A-pre §14 makes activation the instant a timed game's first
    flag deadline is written, and only this service activates a match. The
    store is therefore not optional: a defaulted no-op would mean any caller
    that forgot it silently produced games that never flag — the same
    silent-absence failure `tests/unit/test_reachability.py` exists for.

    Passed in rather than built here because it is **Redis**, not the
    session: AD-21 puts deadlines in a sorted set owned by no node, and this
    function's whole contract is "over one session".
    """
    return MatchAcceptanceService(
        matches=SqlAlchemyMatchRecordRepository(session),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        metrics=metrics,
        deadlines=deadlines,
    )


def build_cooldowns(session: AsyncSession) -> CooldownRepository:
    """The decline cooldown store, over one session — A64-015.5 §3."""
    return SqlAlchemyCooldownRepository(session)


def build_cooldown_audit(session: AsyncSession) -> CooldownAuditRepository:
    """The cooldown audit trail, over one session — A64-015.6 §3.

    A separate factory from `build_cooldowns` because the two are separate
    capabilities: the eligibility check holds the enforcement store and must
    not be able to write history, and only the policy that applies a bar
    holds both.
    """
    return SqlAlchemyCooldownAuditRepository(session)


def build_reconciliation_timeline(
    session: AsyncSession,
) -> ReconciliationTimelineRepository:
    """The recovery timeline, over one session — A64-015.6 §4."""
    return SqlAlchemyReconciliationTimelineRepository(session)


def build_timeline_projector(
    session: AsyncSession, *, clock: Clock
) -> ReconciliationTimelineProjector:
    """The `pairing_reconciled` consumer, over one relay tick's session.

    The cheapest consumer on the relay: one repository, one unit of work, one
    clock, and no cross-module port at all — which is what makes it the one
    least able to be slow, and the reason its `ConsumerPolicy` needs no
    special budget.
    """
    return ReconciliationTimelineProjector(
        timeline=build_reconciliation_timeline(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_match_outcome_service(
    session: AsyncSession,
    *,
    eligibility: QueueEligibilityPolicy,
    events: EventPublisher,
    settings: MatchmakingSettings,
    clock: Clock,
    metrics: MetricsRecorder,
) -> MatchOutcomeService:
    """The acceptance-failure policy, over one relay tick's session —
    A64-015.5 §1.

    Holds a whole `QueueService` rather than a repository, and that is the
    point: the requeue it performs must go through the *use case* — QT-1's
    check, the eligibility gate, the outbox event — rather than through a
    second path that writes tickets its own way. A consumer with a
    repository would be a second implementation of "put somebody in the
    queue", and the two would drift on the first rule that changed.
    """
    return MatchOutcomeService(
        queue=build_queue_service(
            session, eligibility=eligibility, events=events, settings=settings, clock=clock
        ),
        cooldowns=build_cooldowns(session),
        audit=build_cooldown_audit(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        metrics=metrics,
        decline_cooldown_seconds=settings.decline_cooldown_seconds,
    )


def build_pending_match_notifier(
    session: AsyncSession,
    *,
    events: EventPublisher,
    sink: PendingMatchSink,
    clock: Clock,
    metrics: MetricsRecorder,
    deadlines: ClockDeadlineStore,
) -> PendingMatchNotifier:
    """The realtime pending-match consumer, over one relay tick's session —
    A64-015.5 §4.

    Three published reads and a sink. It takes an `EventPublisher` and a
    `ClockDeadlineStore` only to build the acceptance service, which needs
    both — the notifier itself publishes nothing and schedules nothing, and
    a consumer that could would be a consumer that can cause the events it
    reacts to.
    """
    return PendingMatchNotifier(
        acceptance=build_match_acceptance(
            session, events=events, clock=clock, metrics=metrics, deadlines=deadlines
        ),
        exclusions=build_pairing_exclusions(session),
        players=build_opponent_directory(session, clock=clock),
        sink=sink,
        clock=clock,
        metrics=metrics,
    )


def build_queue_retention_service(
    session: AsyncSession,
    *,
    settings: MatchmakingSettings,
    clock: Clock,
    metrics: MetricsRecorder,
) -> QueueRetentionService:
    """One retention run's object graph, over one session — A64-015.5 §8.

    The two stores it holds are the **narrow** ones: neither can resolve a
    ticket or settle a match, which is what keeps a maintenance job from
    being able to change state it is only supposed to remove.

    `game`'s abandoned-match sweep is reached through its published port,
    so this module deletes its own rows and *asks* for the other module's —
    see `game.public.AbandonedMatchRetention` on why the horizon belongs
    here and the rows belong there.
    """
    return QueueRetentionService(
        tickets=SqlAlchemyQueueRetentionStore(session),
        matches=GameAbandonedMatchRetention(
            store=SqlAlchemyMatchRetentionStore(session),
            unit_of_work=SessionUnitOfWork(session),
        ),
        cooldowns=build_cooldowns(session),
        cooldown_audit=build_cooldown_audit(session),
        timeline=build_reconciliation_timeline(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        metrics=metrics,
        policy=queue_retention_policy(
            ticket_retention_hours=settings.ticket_retention_hours,
            abandoned_match_retention_hours=settings.abandoned_match_retention_hours,
            cooldown_retention_hours=settings.cooldown_retention_hours,
            cooldown_audit_retention_hours=settings.cooldown_audit_retention_hours,
            timeline_retention_hours=settings.timeline_retention_hours,
            batch_size=settings.retention_batch_size,
            max_batches=settings.retention_max_batches,
        ),
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
    metrics: MetricsRecorder,
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
        ratings=PublishedRatingProvider(SqlAlchemyRatingReader(session)),
        matches=matches,
        # Built over the **same** session as the repository, which is what
        # puts `PlayersPaired` in the transaction that marks both tickets
        # matched (AD-16).
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        metrics=metrics,
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
    metrics: MetricsRecorder,
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
        metrics=metrics,
        batch_size=settings.reconciliation_batch_size,
    )


def get_metrics() -> MetricsRecorder:
    """The **process-wide** metrics recorder — A64-015.5 §7, A64-015.6 §10.

    `platform.metrics.process_metrics()`, which is the same object the
    composition root wires into every worker path. It used to be a
    `LoggingMetrics` built here, and once A64-015.6 made the recorder
    stateful that second instance stopped being redundancy and started being
    lost counters — see `app/platform/metrics/runtime.py`.

    Returned as the **port**, so a route holding this dependency cannot
    reach `flush()`. Draining is the scheduled task's job and nothing on the
    request path should be able to do it.
    """
    return process_metrics()


MetricsDep = Annotated[MetricsRecorder, Depends(get_metrics)]


def get_clock_deadlines(pools: RedisPoolsDep) -> ClockDeadlineStore:
    """Where a newly activated match's first flag deadline is written —
    A64-020.5A-pre §14.

    The **`live` Redis role**, because that is where AD-21's deadlines live:
    the same sorted set `game`'s own move path writes to, which is what
    makes the deadline an acceptance schedules and the one the first move
    replaces the *same* entry.

    A dependency of its own rather than a line inside
    `get_match_acceptance`, for the reason `get_presence_reader` is one: it
    reads `app.state`, and the contract suite's application has no
    `lifespan` and therefore no Redis. A named dependency is something
    `build_contract_app` can redirect; an inline constructor is not.

    Returned as the **port**, so a route holding this cannot reach
    `claim_expired` — adjudication is the clock worker's job.
    """
    return RedisClockDeadlineStore(pools.live)


ClockDeadlineDep = Annotated[ClockDeadlineStore, Depends(get_clock_deadlines)]


def get_match_acceptance(
    session: DbSessionDep,
    clock: ClockDep,
    events: EventPublisherDep,
    metrics: MetricsDep,
    deadlines: ClockDeadlineDep,
) -> MatchAcceptanceUseCase:
    """The per-request acceptance use case.

    Typed as the **port**, never as `MatchAcceptanceService` — so a route
    annotating this dependency holds three methods and cannot reach
    `expire_overdue` even by accident, though the object in its hand has
    it.
    """
    return build_match_acceptance(
        session, events=events, clock=clock, metrics=metrics, deadlines=deadlines
    )


MatchAcceptanceDep = Annotated[MatchAcceptanceUseCase, Depends(get_match_acceptance)]


def get_opponent_directory(session: DbSessionDep, clock: ClockDep) -> PublicProfileReader:
    """The per-request opponent lookup."""
    return build_opponent_directory(session, clock=clock)


OpponentDirectoryDep = Annotated[PublicProfileReader, Depends(get_opponent_directory)]


__all__ = [
    "EligibilityPolicyDep",
    "EngineServicesDep",
    "MatchAcceptanceDep",
    "ClockDeadlineDep",
    "MetricsDep",
    "OpponentDirectoryDep",
    "PresenceReaderDep",
    "QueueServiceDep",
    "build_eligibility_policy",
    "build_cooldown_audit",
    "build_cooldowns",
    "build_match_acceptance",
    "build_match_creation",
    "build_match_outcome_service",
    "build_opponent_directory",
    "build_pairing_exclusions",
    "build_pairing_service",
    "build_pairing_settlements",
    "build_pending_match_notifier",
    "build_queue_retention_service",
    "build_reconciliation_timeline",
    "build_queue_service",
    "build_rating_window",
    "build_recent_opponents",
    "build_reconciliation_service",
    "build_timeline_projector",
    "get_eligibility_policy",
    "get_engine_services",
    "get_match_acceptance",
    "get_clock_deadlines",
    "get_metrics",
    "get_opponent_directory",
    "get_presence_reader",
    "get_queue_service",
]


def build_challenge_service(
    session: AsyncSession,
    *,
    clock: Clock,
    cache: SocialGraphCache,
    events: EventPublisher,
) -> ChallengeService:
    """The friend challenge use cases, over one unit of work — A64-022.1 §25.

    Named concretely here, which is what a composition root is for: the
    service itself holds only ports — `ChallengeRepository`,
    `SocialGraphReader`, `PairingExclusions`, `TimeControlCatalogue` — and so
    cannot reach into `friends` or `reference` for anything else.

    **The social graph arrives through `CachedSocialGraphReader`**, the same
    decorator the profile path and the notification relay use. Not for speed
    here — a challenge asks about one player — but so that this entry point
    and every other cannot disagree about what the graph says. A friendship
    revoked a second ago must be revoked for a challenge too, and a second
    uncached reader would be a second answer.

    **Not a `Depends`.** A64-022.1 has no HTTP surface (§16 forbids one), so
    a request-scoped factory would be a signature nothing resolves. It takes
    a session because it is built per unit of work: it holds a repository, a
    repository holds a session, and a session must not outlive the work it
    serves.
    """
    return ChallengeService(
        challenges=SqlAlchemyChallengeRepository(session),
        social_graph=CachedSocialGraphReader(
            SocialGraphReaderService(
                friendships=SqlAlchemyFriendshipRepository(session),
                blocks=SqlAlchemyBlockedPlayerRepository(session),
            ),
            cache,
        ),
        exclusions=build_pairing_exclusions(session),
        # `reference`'s own adapter, so the catalogue a challenge validates
        # against is the one the queue offers. Two readers would be two
        # menus.
        time_controls=SqlAlchemyTimeControlCatalogue(session),
        # A64-022.3. `game`'s own use case, over the **same session** — which
        # is what makes acceptance atomic: `SessionUnitOfWork` is a scope
        # marker, so the challenge update staged before this commits with the
        # match it creates. A second session would be two transactions and a
        # window in which a match exists with no accepted challenge.
        matches=PersistentMatchCreation(
            matches=SqlAlchemyMatchRecordRepository(session),
            events=events,
            # **Participating, not owning** — A64-022.3 §10. `game`'s use
            # case commits by contract, which is right for every caller that
            # only creates a match and wrong for acceptance: the match, the
            # challenge transition and both events must land together.
            #
            # Handing it a unit of work that stages and flushes rather than
            # commits is what composes the two without special-casing either
            # — `game` is unchanged and its other callers are unchanged.
            unit_of_work=ParticipatingUnitOfWork(session),
            clock=clock,
        ),
        # The same provider the queue reads seats through, so a challenge
        # game and a queue game record a rating identically.
        ratings=PublishedRatingProvider(SqlAlchemyRatingReader(session)),
        # A64-022.2. The outbox, over the **same session** as the repository:
        # a challenge and the event announcing it commit together or neither
        # does (AD-16). A publisher on a second session would be an event
        # that survived a rolled-back challenge.
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_challenge_expiry_service(
    session: AsyncSession,
    *,
    settings: MatchmakingSettings,
    clock: Clock,
    metrics: MetricsRecorder,
    events: EventPublisher,
) -> ChallengeExpiryService:
    """One expiry sweep's object graph, over one session — A64-022.6 §2.

    Deliberately **much narrower** than `build_challenge_service` above: a
    repository, the outbox, a clock and a counter. No social graph, no time
    control catalogue, no match creation, no rating provider.

    That is the point rather than an economy. Expiry is the one challenge
    transition with no actor and no negotiation — nothing is validated
    against a relationship, nothing is looked up in a catalogue, and no game
    is created — so a sweep able to name `MatchCreationUseCase` would be a
    scheduled job that could produce a match. §19 asks whether an actorless
    scheduler can bypass a domain invariant, and the honest answer is that
    it cannot reach the collaborators it would need to.

    The publisher is over the **same session** as the repository, so the
    transition and the event announcing it commit together or neither does
    (AD-16).
    """
    return ChallengeExpiryService(
        challenges=SqlAlchemyChallengeRepository(session),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        metrics=metrics,
        batch_size=settings.challenge_expiry_batch_size,
    )


def get_challenge_service(
    session: DbSessionDep,
    clock: ClockDep,
    cache: SocialGraphCacheDep,
    events: EventPublisherDep,
) -> ChallengeService:
    """The same graph, assembled for one request — A64-022.2.

    A64-022.1 had no HTTP surface, so `build_challenge_service` took its
    collaborators positionally and nothing resolved them. This is the
    `Depends` form the router uses; the builder stays because the contract
    suite and any future worker reach it without a request to resolve
    against.
    """
    return build_challenge_service(session, clock=clock, cache=cache, events=events)


ChallengeServiceDep = Annotated[ChallengeService, Depends(get_challenge_service)]
