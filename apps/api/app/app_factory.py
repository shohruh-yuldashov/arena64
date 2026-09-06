"""The application factory — the composition root for the HTTP runtime
profile (architecture.md AD-02).

Uses `lifespan`, not the deprecated `@app.on_event("startup"/"shutdown")`
hooks: everything a request handler reaches through `app.state` or
`Depends` is built in one place, in a defined order, with a matching
teardown — not scattered across decorators that run in registration order
with no enforced pairing.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import service_lifecycle
from app.api.exception_handlers import register_exception_handlers
from app.api.http_metrics import REQUESTS_IN_FLIGHT, HttpMetricsMiddleware, InFlight
from app.api.observability import build_metrics_router
from app.api.router import api_router
from app.api.v1.health import build_drain_route, health_router
from app.common.logging import configure_logging
from app.common.middleware import CorrelationIdMiddleware, RequestIdMiddleware
from app.config.environment import current_environment, describe_env_file
from app.config.settings import Settings, get_settings
from app.core.clock import SystemClock
from app.core.constants import API_PREFIX
from app.database.rate_limiter import RedisRateLimiter
from app.database.redis import RedisPools, create_redis_pools
from app.database.session_manager import DatabaseSessionManager
from app.database.unit_of_work import SessionUnitOfWork
from app.gateway.dependencies import (
    build_broadcaster_for,
    get_gateway_bus_for,
    get_local_sockets,
)
from app.gateway.forwarding import GatewayForwarder
from app.gateway.forwarding_tasks import GatewayForwardingTask, forwarding_request
from app.gateway.matchmaking_offers import GatewayPendingMatchSink
from app.gateway.node import resolve_node_id
from app.gateway.notifications import GatewayNotificationSink
from app.gateway.router import gateway_router
from app.modules.analytics.application.services.projections import (
    PROJECTIONS as ANALYTICS_PROJECTIONS,
)
from app.modules.analytics.application.services.projector import (
    CONSUMER as ANALYTICS_CONSUMER,
)
from app.modules.analytics.application.services.projector import (
    AnalyticsProjector,
)
from app.modules.analytics.application.services.retention_task import (
    AnalyticsRetentionTask,
)
from app.modules.analytics.application.services.retention_task import (
    prune_request as analytics_prune_request,
)
from app.modules.analytics.infrastructure.repositories.analytics_repository import (
    SqlAlchemyAnalyticsEventStore,
    SqlAlchemySubjectDirectory,
)
from app.modules.friends.application.ports import SocialGraphCache
from app.modules.friends.infrastructure.cache import (
    NoSocialGraphCache,
    RedisSocialGraphCache,
)
from app.modules.game.application.services import ClockAdjudicationService
from app.modules.game.application.services.clock_reconciliation import (
    ClockDeadlineReconciliationTask,
)
from app.modules.game.application.services.clock_reconciliation import (
    reconcile_request as clock_reconcile_request,
)
from app.modules.game.application.services.match_abort_service import PersistentMatchAbort
from app.modules.game.application.services.origin_match_service import GameOriginMatches
from app.modules.game.infrastructure import (
    ClockAdjudicationTask,
    RedisClockDeadlineStore,
    adjudication_request,
)
from app.modules.game.infrastructure.repositories import SqlAlchemyMatchRecordRepository
from app.modules.matchmaking.application.ports import PendingMatchSink
from app.modules.matchmaking.application.services import (
    MatchOutcomeService,
    PairingReconciliationService,
    PairingService,
    PendingMatchNotifier,
    QueueRetentionService,
    QueueService,
)
from app.modules.matchmaking.application.services.challenge_expiry_service import (
    ChallengeExpiryService,
)
from app.modules.matchmaking.application.services.match_outcome_service import (
    CONSUMER_NAME as ACCEPTANCE_FAILURE_CONSUMER,
)
from app.modules.matchmaking.application.services.match_outcome_service import (
    SUBSCRIBED_EVENT_TYPES as ACCEPTANCE_FAILURE_EVENTS,
)
from app.modules.matchmaking.application.services.pending_match_notifier import (
    CONSUMER_NAME as PENDING_MATCH_CONSUMER,
)
from app.modules.matchmaking.application.services.pending_match_notifier import (
    SUBSCRIBED_EVENT_TYPES as PENDING_MATCH_EVENTS,
)
from app.modules.matchmaking.application.services.reconciliation_timeline_service import (
    CONSUMER_NAME as TIMELINE_CONSUMER,
)
from app.modules.matchmaking.application.services.reconciliation_timeline_service import (
    SUBSCRIBED_EVENT_TYPES as TIMELINE_EVENTS,
)
from app.modules.matchmaking.domain.queue_pool import every_pool
from app.modules.matchmaking.infrastructure import (
    ChallengeExpiryTask,
    LoggingPendingMatchSink,
    PairingReconciliationTask,
    PairingTask,
    QueueExpiryTask,
    QueueRetentionTask,
    challenge_expiry_request,
    expiry_request,
    pairing_request,
    queue_retention_request,
    reconciliation_request,
)
from app.modules.matchmaking.presentation.dependencies import (
    build_challenge_expiry_service,
    build_eligibility_policy,
    build_match_acceptance,
    build_match_creation,
    build_match_outcome_service,
    build_pairing_exclusions,
    build_pairing_service,
    build_pairing_settlements,
    build_pending_match_notifier,
    build_queue_retention_service,
    build_queue_service,
    build_recent_opponents,
    build_reconciliation_service,
    build_timeline_projector,
)
from app.modules.notifications.application.services import (
    CONSUMER_NAME,
    SUBSCRIBED_EVENT_TYPES,
    SocialNotificationDispatcher,
)
from app.modules.notifications.application.services.challenge_notification_dispatcher import (
    CONSUMER_NAME as CHALLENGE_NOTIFICATION_CONSUMER,
)
from app.modules.notifications.application.services.challenge_notification_dispatcher import (
    SUBSCRIBED_EVENT_TYPES as CHALLENGE_NOTIFICATION_EVENTS,
)
from app.modules.notifications.application.services.game_notification_dispatcher import (
    CONSUMER_NAME as GAME_NOTIFICATION_CONSUMER,
)
from app.modules.notifications.application.services.game_notification_dispatcher import (
    SUBSCRIBED_EVENT_TYPES as GAME_NOTIFICATION_EVENTS,
)
from app.modules.notifications.application.services.presence_sweeper import PresenceSweeper
from app.modules.notifications.application.services.tournament_notification_dispatcher import (
    CONSUMER_NAME as TOURNAMENT_NOTIFICATION_CONSUMER,
)
from app.modules.notifications.application.services.tournament_notification_dispatcher import (
    SUBSCRIBED_EVENT_TYPES as TOURNAMENT_NOTIFICATION_EVENTS,
)
from app.modules.notifications.infrastructure import (
    CompositeNotificationSink,
    LoggingNotificationSink,
    PresenceSweeperWorker,
    SessionScopedNotificationHandler,
)
from app.modules.notifications.infrastructure.tasks import (
    NotificationBroadcastTask,
    NotificationEmailDeliveryTask,
    NotificationPushDeliveryTask,
    broadcast_delivery_request,
    email_delivery_request,
    push_delivery_request,
)
from app.modules.notifications.presentation.dependencies import (
    build_broadcast_expander,
    build_challenge_notification_dispatcher,
    build_durable_notification_writer,
    build_email_delivery_service,
    build_game_notification_dispatcher,
    build_push_delivery_service,
    build_social_notification_dispatcher,
    build_tournament_notification_dispatcher,
    channel_availability_for,
    email_channel_available,
)
from app.modules.profiles.presentation.dependencies import build_profile_renderer
from app.modules.rating.application.services.match_completion_consumer import (
    CONSUMER_NAME as RATING_CONSUMER,
)
from app.modules.rating.application.services.match_completion_consumer import (
    MATCH_COMPLETED,
    MatchCompletionConsumer,
)
from app.modules.rating.application.services.match_rating_service import MatchRatingService
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyPlayerRatingRepository,
)
from app.modules.statistics.application.services.match_completion_consumer import (
    CONSUMER_NAME as STATISTICS_CONSUMER,
)
from app.modules.statistics.application.services.match_completion_consumer import (
    StatisticsMatchCompletionConsumer,
)
from app.modules.statistics.application.services.match_projection_service import (
    MatchProjectionService,
)
from app.modules.statistics.infrastructure.repositories.statistics_repository import (
    SqlAlchemyStatisticsRepository,
)
from app.modules.tournament.application.services.match_completion_consumer import (
    CONSUMER_NAME as TOURNAMENT_CONSUMER,
)
from app.modules.tournament.application.services.match_completion_consumer import (
    TournamentMatchCompletionConsumer,
)
from app.modules.tournament.application.services.no_show_service import (
    TournamentNoShowService,
)
from app.modules.tournament.application.services.reconciliation_service import (
    TournamentReconciliationService,
)
from app.modules.tournament.infrastructure.tasks import (
    TournamentDeadlineTask,
    TournamentNoShowTask,
    TournamentReconciliationTask,
)
from app.modules.tournament.infrastructure.tasks import (
    deadline_request as tournament_deadline_request,
)
from app.modules.tournament.infrastructure.tasks import (
    no_show_request as tournament_no_show_request,
)
from app.modules.tournament.infrastructure.tasks import (
    reconciliation_request as tournament_reconciliation_request,
)
from app.modules.tournament.presentation.dependencies import (
    build_deadline_service,
    build_match_completion_consumer,
    build_no_show_service,
    build_notification_reader,
)
from app.modules.tournament.presentation.dependencies import (
    # Aliased: `matchmaking` publishes a factory of the same name for its
    # own reconciler, and the two recover different things.
    build_reconciliation_service as build_tournament_reconciliation,
)
from app.modules.users.infrastructure.presence import (
    NoPresenceProvider,
    RedisPresenceProvider,
)
from app.operator import backup_status, certificate_status
from app.platform.email import build_email_provider
from app.platform.metrics import (
    AggregatingMetrics,
    MetricsFlushTask,
    flush_request,
    process_metrics,
    prometheus_metrics,
)
from app.platform.metrics.loop import EventLoopLagProbe
from app.platform.metrics.prometheus import PrometheusMetrics
from app.platform.outbox import (
    ConsumerPolicies,
    ConsumerPolicy,
    EventHandler,
    OutboxEventPublisher,
    OutboxRetentionTask,
    OutboxWorker,
    SqlAlchemyOutboxRepository,
    prune_request,
    retention_policy,
)
from app.platform.outbox.entry import BacklogSnapshot, process_backlog
from app.platform.outbox.metrics import BACKLOG, OLDEST_PENDING_AGE
from app.platform.push import build_push_provider, build_vapid_keys
from app.platform.tasks import (
    InlineTaskDispatcher,
    PeriodicTaskScheduler,
    TaskHandler,
)
from app.storage import LocalStorageProvider, S3StorageProvider

logger = logging.getLogger(__name__)

#: Tag metadata for the generated docs — A64-011.9.
#:
#: Without this, `/docs` renders bare tag names above each group and a
#: reader has to infer from the endpoint list what "auth" contains and how
#: it differs from "users". The two are genuinely easy to confuse on this
#: platform, because the split is a *bounded context* boundary rather than
#: an obvious one: `auth` owns proving who you are, `users` owns who you
#: appear to be (architecture.md §6, DM-10). Saying so once, here, is the
#: cheapest place to prevent an endpoint being added to the wrong module.
#:
#: FastAPI renders only tags it finds here *and* on a route, so a tag added
#: to a router without an entry below is not an error — it simply gets no
#: description, exactly as before.
OPENAPI_TAGS: list[dict[str, Any]] = [
    {
        "name": "auth",
        "description": (
            "Proving identity: registration, sign-in, token issue and rotation, "
            "email verification and password recovery.\n\n"
            "Two credentials, and they are not interchangeable. The **access token** "
            "is a short-lived JWT sent as `Authorization: Bearer <token>` and cannot "
            "be revoked before it expires. The **refresh token** is an opaque, "
            "single-use value sent in the request body of `POST /auth/refresh`; it "
            "is rotated on every use, and presenting one that has already been "
            "rotated revokes the entire session chain.\n\n"
            "Endpoints that accept an address or send mail answer identically "
            "whether or not an account exists — deliberately, so that none of them "
            "can be used to discover which addresses are registered."
        ),
    },
    {
        "name": "users",
        "description": (
            "Resolving a player **id** to a public identity — who someone *appears "
            "to be*, as opposed to `auth`, which owns proving who they *are*. A "
            "player can exist without an account (a bot seat, a guest), which is why "
            "the two are separate contexts rather than one aggregate.\n\n"
            "Deliberately thin: a handle, a display name and two avatar URLs. "
            "Everything a privacy setting governs — country, statistics, presence — "
            "is on `GET /profiles/{username}` instead, so these two routes have "
            "nothing to gate and cannot drift from the rules that profile applies.\n\n"
            "Listings cover **active accounts only**. A deactivated account does not "
            "appear in any page, which matches `GET /profiles/{username}` answering "
            "`404` for one: which handles belong to withdrawn accounts is exactly "
            "what an impersonator wants to know."
        ),
    },
    {
        "name": "profiles",
        "description": (
            "Public player profiles, read by handle and by anyone. A profile is a "
            "*composition* rather than a record: identity comes from `users`, ratings "
            "from the rating system, match counts from statistics and presence from "
            "the realtime tier.\n\n"
            "Ratings currently report placeholder values — a provisional starting "
            "rating — because no game has been played on this platform yet, and "
            "presence reports `null` for everyone because the gateway that records it "
            "does not exist yet. The **shape** is final, so a client written against it "
            "today needs no change when real values arrive.\n\n"
            "**A `null` never explains itself.** `country`, `statistics`, `is_online` "
            "and `last_seen` are all `null` both when a player has hidden them and "
            "when the platform simply has nothing to report, and the two are "
            "deliberately indistinguishable — saying which applies would answer the "
            "question the privacy setting exists to decline."
        ),
    },
    {
        "name": "profile",
        "description": (
            "Your own profile, privacy settings and preferences — reading them and "
            "changing them. Every endpoint acts on the account behind your access "
            "token; there is no path segment or body field naming an account, so "
            "another player's profile cannot be addressed here.\n\n"
            "Three groups, three endpoints, deliberately not merged. `PATCH /profile` "
            "changes what you *say about yourself* — display name, biography, "
            "country. `PATCH /profile/privacy` changes what *strangers may see*. "
            "`PATCH /profile/preferences` changes what *you* see, and is the only "
            "place your interface language and timezone can be set.\n\n"
            "**Username and email are not editable anywhere** — changing either has "
            "consequences a profile edit does not (a rename must reserve the old "
            "handle; an email change must re-prove ownership), so each gets its own "
            "flow. Your avatar is at `/profile/avatar`.\n\n"
            "**Nothing here is ever redacted.** Privacy settings govern what "
            "`GET /profiles/{username}` shows a stranger, never what you see of "
            "yourself — a control that hid a value from the person who set it would "
            "be one nobody could verify they had applied."
        ),
    },
    {
        "name": "search",
        "description": (
            "Finding players by handle or display name — the entry point to everything "
            "social.\n\n"
            "**The one profile read that requires a token.** Every other public view of "
            "a player is anonymous: if you know a handle, you can read that profile "
            "signed out. Discovering handles you do not know is different, and is what "
            "this endpoint is, so it sits behind authentication and a per-account rate "
            "limit — which together make building a directory cost an attacker a "
            "registration per budget.\n\n"
            "Results are the **same** representation `GET /profiles/{username}` "
            "returns, field for field, including every privacy behaviour: a hidden "
            "country, record or presence is `null` here exactly as it is there, and a "
            "`null` never says which of its reasons applies. Deactivated accounts "
            "appear at no rank under any term."
        ),
    },
    {
        "name": "avatars",
        "description": (
            "Self-service avatar management for the authenticated account. Every "
            "endpoint acts on **your own** avatar — there is no path segment or body "
            "field naming an account, so another player's avatar cannot be addressed "
            "at all.\n\n"
            "Uploads are validated by file signature rather than by the declared "
            "`Content-Type`, re-encoded to WebP from decoded pixels (which strips EXIF "
            "and every other metadata block), and stored in two sizes. Read anyone's "
            "avatar URL from `GET /profiles/{username}` instead."
        ),
    },
    {
        "name": "friends",
        "description": (
            "Friend requests — the first half of the social graph (A64-013.2). "
            "Sending, listing what you have sent and received, and the three ways a "
            "request ends: accepted, declined, cancelled.\n\n"
            "**Every endpoint acts as the account behind your access token.** No "
            "path, query or body field names who is sending, accepting, declining or "
            "cancelling, so acting as somebody else is not something the API can "
            "express.\n\n"
            "Only the *recipient* may accept or decline; only the *sender* may "
            "cancel. The two are not interchangeable — they leave different history, "
            "and a future decline cooldown reads it.\n\n"
            "**Nothing is ever deleted.** `DELETE /friends/requests/{id}` resolves a "
            "request to `cancelled`; accepted, declined and cancelled rows are kept, "
            "because a request that ended is a fact with a date.\n\n"
            "A declined request is **silent to the sender**: it simply leaves their "
            "outgoing list, with no notification and no explanation.\n\n"
            "Accepting does not yet produce a friend list — that is A64-013.3, which "
            "creates the friendship in the same transaction that resolves the "
            "request."
        ),
    },
    {
        "name": "blocks",
        "description": (
            "Blocking — a platform-wide policy rather than a feature (A64-013.5).\n\n"
            "**The blocked player is never told.** Nothing notifies them, no response "
            "of theirs changes shape, and no endpoint anywhere reports being blocked. "
            "A visible block is an invitation to retaliate from a second account.\n\n"
            "Blocking takes effect everywhere at once, in one transaction: any "
            "friendship ends, any pending friend request in either direction is "
            "voided, new requests are refused, each disappears from the other's "
            "search results, and neither sees the other's presence or "
            "audience-restricted profile fields.\n\n"
            "**A block outranks every privacy setting**, including `everyone`: a "
            "player who published a field and then blocked somebody has not published "
            "it to them.\n\n"
            "The effect is symmetric even though the fact is not. A block is "
            "one-directional and only the blocker can lift it — but neither party can "
            "tell which of them placed it, which is what keeps 'am I blocked' "
            "unanswerable.\n\n"
            "**Nothing is restored on unblock.** A friendship a block ended stays "
            "ended and a voided request stays voided; the two must start again."
        ),
    },
    {
        "name": "matchmaking",
        "description": (
            "Waiting for an opponent, and answering when one is found (A64-014.1, "
            "A64-015.4).\n\n"
            "**Two halves, and a client needs both.** Joining puts a ticket in a pool; "
            "a background scan pairs waiting players and creates a match for them. "
            "When that happens your ticket stops existing — `GET /matchmaking/queue/me` "
            "answers `404` — and the offer appears at "
            "`GET /matchmaking/matches/pending`, where you have a few seconds to accept "
            "or decline. A match starts only when **both** of you accept.\n\n"
            "**A declined or unanswered match returns nobody to the queue.** Your "
            "ticket was consumed when the match was created, so both players rejoin by "
            "hand. That is deliberate and provisional: what a declined acceptance "
            "should cost each side is an open product question.\n\n"
            "**One ticket per player, across every pool.** Joining `casual` while "
            "waiting in `ranked` is a `409`, not a second ticket: being paired into two "
            "simultaneous matches means abandoning one, which looks to that opponent "
            "exactly like a stolen win.\n\n"
            "**Every endpoint acts as the account behind your access token.** No path, "
            "query or body field names who is joining, leaving or being read, so "
            "queueing as somebody else is not something this API can express — and "
            "there is deliberately no endpoint that reads another player's ticket, "
            "because who is queueing right now is what would let somebody wait for a "
            "favourable pool.\n\n"
            "**Your rating is not yours to send.** A ticket records the rating the "
            "platform holds for you at the moment you join, and it is fixed for that "
            "ticket's life — a rating that changes while you wait does not move your "
            "place. Every rating is provisional today, because no game has been played "
            "here yet.\n\n"
            "Tickets expire. `expires_at` is an instant rather than a countdown, and a "
            "ticket past it reads as absent immediately — even in the moment before a "
            "background worker records it."
        ),
    },
    {
        "name": "health",
        "description": (
            "Liveness and readiness probes for load balancers and orchestrators. "
            "Unversioned and unauthenticated: a probe must not sit behind API "
            "versioning that could itself fail to resolve."
        ),
    },
]


def build_outbox_worker(
    db: DatabaseSessionManager, redis_pools: RedisPools, settings: Settings
) -> OutboxWorker | None:
    """The relay worker for this process, or `None` if it does not run one.

    Assembled here rather than in `notifications` because it composes three
    modules — the social graph and the audience from `friends`, the renderer
    from `profiles`, the sink from `notifications` — and composing modules is
    the composition root's job (BR-6 forbids a *module* reaching for the
    container, not the root wiring modules together).

    The dispatcher is built **per tick**, inside the handler wrapper below,
    for the reason every service is built per request: it holds repositories,
    repositories hold a session, and a session must not outlive the unit of
    work it serves. What is long-lived here is the worker and its session
    factory, which is the same lifetime `app.state.db` has.
    """
    if not settings.outbox.worker_enabled:
        return None

    # A64-021.5. The relay's writers enqueue an email alongside every
    # notification they insert, and only when this process can send one —
    # see `DurableNotificationWriter.store`. Read once here rather than per
    # tick, because it is a configuration reading and cannot change under a
    # running process.
    channel_availability = channel_availability_for(settings)

    clock = SystemClock()

    # A64-013.7's seam, and no longer the *only* sink: A64-021.1 puts
    # `DurableNotificationWriter` in front of it, so a social notification is
    # now stored as well as recorded. This one stays because it covers the
    # transient kinds the durable writer deliberately does not keep —
    # presence, which would flood the table — and because "a delivery
    # happened" is worth a log line whether or not a row was written.
    #
    # Process-wide, unlike the durable writer, because it holds nothing: no
    # repository, no session, nothing per tick.
    sink = LoggingNotificationSink()

    # A64-015.5's equivalent seam, for a different payload shape — and
    # A64-020.5D is the day AD-09's gateway exists, so this is where the
    # real transport is wired.
    #
    # One instance per process. It holds the fleet fan-out, which holds the
    # process-wide socket registry and the bus, and rebuilding it per relay
    # tick would be rebuilding the transport.
    #
    # **`LoggingPendingMatchSink` is no longer the production default**
    # (§10). It survives behind an explicit switch for local diagnostics
    # and for the tests whose subject is the resolution rather than the
    # delivery — never as a silent fallback, which is the failure mode §10
    # names: a deployment that believes it pushes and only logs.
    pending_match_sink: PendingMatchSink = (
        GatewayPendingMatchSink(
            broadcaster=build_broadcaster_for(
                pools=redis_pools,
                settings=settings.gateway,
                clock=clock,
                node_id=resolve_node_id(settings.gateway),
            ),
            metrics=_metrics(),
        )
        if settings.gateway.match_offer_push_enabled
        else LoggingPendingMatchSink()
    )
    # A64-021.2. The notification announcer, and **one instance per
    # process** for the reason the offer sink is: it holds the fleet fan-out,
    # which holds the process-wide socket registry and the bus, and
    # rebuilding it per relay tick would be rebuilding the transport.
    #
    # No configuration switch, unlike `match_offer_push_enabled`, and the
    # asymmetry is deliberate. That flag exists because a match offer push
    # replaces a poll a lobby depends on, so an operator needs a way back to
    # the polled path. This one replaces nothing: the notification list and
    # the unread count still refetch on focus exactly as they did (§6), the
    # announcer never raises, and the row it announces is already committed.
    # A switch here would turn off a latency improvement and nothing else,
    # which is a knob with no incident behind it.
    notification_announcer = GatewayNotificationSink(
        broadcaster=build_broadcaster_for(
            pools=redis_pools,
            settings=settings.gateway,
            clock=clock,
            node_id=resolve_node_id(settings.gateway),
        ),
        metrics=_metrics(),
    )

    if not settings.gateway.match_offer_push_enabled:
        # `WARNING`, not `INFO`: with it off a paired player learns they
        # were matched only when their lobby next polls, which is a
        # degraded product rather than a configuration detail.
        logger.warning("match_offer_push_disabled", extra={"reason": "configuration"})

    def dispatcher_for(session: AsyncSession) -> SocialNotificationDispatcher:
        """The consumer, over one relay tick's session.

        A closure defined here rather than a method on the handler, because
        this is the **only** place permitted to name three modules' concrete
        classes at once (BR-6: a module must not reach for the container; the
        root wiring modules together is the root's job). A64-013.8 moved it
        out of `notifications.infrastructure`, where it was three boundary
        violations that an import contract then caught.

        The cache is built per call and shared by the dispatcher's graph
        reader and its profile renderer, so a block set read while resolving
        an audience and one read while rendering are the same read.
        """
        cache: SocialGraphCache = (
            RedisSocialGraphCache(redis_pools.cache, settings=settings.friends)
            if settings.friends.cache_enabled
            else NoSocialGraphCache()
        )
        return build_social_notification_dispatcher(
            session,
            cache=cache,
            profiles=build_profile_renderer(
                session,
                pools=redis_pools,
                settings=settings,
                cache=cache,
                clock=clock,
            ),
            # A64-021.1. **Durable first, log second**, and the order is the
            # contract: a line saying a notification was delivered is only
            # ever written after the row that makes NT-1 true exists.
            #
            # The durable writer is built here, per tick, because it holds a
            # repository over this tick's session — unlike the logging sink
            # above, which is process-wide because it holds nothing.
            sink=CompositeNotificationSink(
                [
                    build_durable_notification_writer(
                        session,
                        announcer=notification_announcer,
                        availability=channel_availability,
                    ),
                    sink,
                ]
            ),
        )

    handler = SessionScopedNotificationHandler(
        session_factory=db.session_factory,
        dispatcher_factory=dispatcher_for,
        consumer=CONSUMER_NAME,
        event_types=SUBSCRIBED_EVENT_TYPES,
    )

    # A64-015.5. Two `matchmaking` consumers join the relay, and both are
    # wrapped in the same session-scoping adapter `notifications` already
    # uses: they hold repositories, repositories hold a session, and a
    # session must not outlive the unit of work it serves.
    #
    # **Each has its own `processed_event` partition**, so the ledger keeps
    # them independently idempotent — a redelivery that the realtime
    # notifier has already handled can still reach the acceptance-failure
    # policy, and neither can mark the other's work done.
    # `EventHandler`, and stated as such — A64-028.4 §18, §19.
    #
    # This was `list[TaskHandler | object]`, and the `| object` was not a
    # convenience: it turned off the one check that would have caught what
    # happened next. `AnalyticsRetentionTask` is a `TaskHandler` — it has
    # `name` and `run`, the scheduler's protocol — and it was appended here,
    # to the *relay's* handlers, which need `handles` and `handle`.
    #
    # The relay then called `handles()` on it on **every tick**, raised
    # `AttributeError`, and failed the whole pass. Not one consumer: the
    # pass. So nothing the outbox carries — notifications, rating
    # application, analytics projections, tournament and social events — was
    # delivered by any process running this code.
    #
    # With the annotation honest, mypy refuses the same mistake at the line
    # that makes it.
    handlers: list[EventHandler] = [handler]
    handlers.append(
        SessionScopedNotificationHandler(
            session_factory=db.session_factory,
            dispatcher_factory=lambda session: _match_outcome_for(
                session, redis_pools, settings, clock
            ),
            consumer=ACCEPTANCE_FAILURE_CONSUMER,
            event_types=ACCEPTANCE_FAILURE_EVENTS,
        )
    )
    handlers.append(
        SessionScopedNotificationHandler(
            session_factory=db.session_factory,
            dispatcher_factory=lambda session: build_timeline_projector(session, clock=clock),
            consumer=TIMELINE_CONSUMER,
            event_types=TIMELINE_EVENTS,
        )
    )
    # A64-017.6, closing the seam A64-017.3 left open. `MatchRatingService`
    # was built and had no caller, so a match completed, the outbox row was
    # written, and **no rating on this platform ever moved** — every part
    # working and the feature absent, which is the same failure A64-016.8
    # found in the cross-node bus.
    #
    # Its own `processed_event` partition, like every consumer here: a
    # redelivery the timeline projector has handled must still reach this
    # one, and neither may mark the other's work done.
    handlers.append(
        SessionScopedNotificationHandler(
            session_factory=db.session_factory,
            dispatcher_factory=lambda session: _rating_consumer_for(session, clock),
            consumer=RATING_CONSUMER,
            event_types=frozenset({MATCH_COMPLETED}),
        )
    )
    # A64-019.5. The other half of A64-019.0: `game` hands `origin_ref` back
    # on completion, and this is what recognises the match as a tournament's
    # own and moves the bracket. Without it a tournament creates matches and
    # never advances — the same shape of silent absence the rating consumer
    # above was added to close.
    #
    # Its **own** `processed_event` partition, like every consumer here. It
    # subscribes to the same event the rating consumer does, and neither may
    # mark the other's work done: a redelivery the ladder has already
    # applied must still reach the bracket.
    handlers.append(
        SessionScopedNotificationHandler(
            session_factory=db.session_factory,
            dispatcher_factory=lambda session: _tournament_consumer_for(session, settings, clock),
            consumer=TOURNAMENT_CONSUMER,
            event_types=frozenset({MATCH_COMPLETED}),
        )
    )
    # A64-020.5F. `statistics` shipped as the *reading* half of a
    # projection, because "the writing half is a consumer of
    # `match.completed` and there is no `game` module to emit one". There is
    # now — and until this line existed, `player_statistics` was empty in
    # every environment while `rating` consumed the same event 74 times. A
    # player finished a game, saw their rating move, and saw their match
    # count stay at zero.
    #
    # Its **own** `processed_event` partition, like every consumer here.
    # Three modules now subscribe to `game.match_completed`, and none may
    # mark another's work done.
    handlers.append(
        SessionScopedNotificationHandler(
            session_factory=db.session_factory,
            dispatcher_factory=lambda session: _statistics_consumer_for(session, clock),
            consumer=STATISTICS_CONSUMER,
            event_types=frozenset({MATCH_COMPLETED}),
        )
    )
    # A64-021.4. Two more notification consumers, and the reason they are
    # separate from `social_notifications` rather than event types added to
    # it: each holds a different collaborator — one `tournament`'s published
    # reader, one `profiles`' renderer — and a single dispatcher would hold
    # both to serve either. They also fail independently, which is the
    # property that matters on a relay: a tournament whose standings are not
    # yet visible must not stall a finished game's notification.
    #
    # **Each has its own `processed_event` partition**, like every consumer
    # here. `game_notifications` is the fourth subscriber to
    # `game.match_completed`; none may mark another's work done.
    handlers.append(
        SessionScopedNotificationHandler(
            session_factory=db.session_factory,
            dispatcher_factory=lambda session: build_tournament_notification_dispatcher(
                tournaments=build_notification_reader(session),
                # The same writer the social path uses, built the same way:
                # per tick, over this tick's session, with the announcer the
                # fleet shares. That is what makes A64-021.3's preference
                # suppression and A64-021.2's realtime frame apply to a
                # tournament notification without either being re-implemented.
                store=build_durable_notification_writer(
                    session,
                    announcer=notification_announcer,
                    availability=channel_availability,
                ),
            ),
            consumer=TOURNAMENT_NOTIFICATION_CONSUMER,
            event_types=TOURNAMENT_NOTIFICATION_EVENTS,
        )
    )
    handlers.append(
        SessionScopedNotificationHandler(
            session_factory=db.session_factory,
            dispatcher_factory=lambda session: build_game_notification_dispatcher(
                profiles=build_profile_renderer(
                    session,
                    pools=redis_pools,
                    settings=settings,
                    cache=NoSocialGraphCache()
                    if not settings.friends.cache_enabled
                    else RedisSocialGraphCache(redis_pools.cache, settings=settings.friends),
                    clock=clock,
                ),
                store=build_durable_notification_writer(
                    session,
                    announcer=notification_announcer,
                    availability=channel_availability,
                ),
            ),
            consumer=GAME_NOTIFICATION_CONSUMER,
            event_types=GAME_NOTIFICATION_EVENTS,
        )
    )
    # A64-022.4. The friend challenge consumer, and the first time
    # `notifications` names `matchmaking` at all — see `.importlinter`'s
    # `matchmaking-is-not-a-dependency` on why that relaxation is one
    # package wide and carries no capability.
    #
    # Its **own** `processed_event` partition, like every consumer here.
    # Nothing else subscribes to the challenge events today; the partition
    # exists so that when something does, neither can mark the other's work
    # done.
    handlers.append(
        SessionScopedNotificationHandler(
            session_factory=db.session_factory,
            dispatcher_factory=lambda session: build_challenge_notification_dispatcher(
                session,
                cache=NoSocialGraphCache()
                if not settings.friends.cache_enabled
                else RedisSocialGraphCache(redis_pools.cache, settings=settings.friends),
                profiles=build_profile_renderer(
                    session,
                    pools=redis_pools,
                    settings=settings,
                    cache=NoSocialGraphCache()
                    if not settings.friends.cache_enabled
                    else RedisSocialGraphCache(redis_pools.cache, settings=settings.friends),
                    clock=clock,
                ),
                store=build_durable_notification_writer(
                    session,
                    announcer=notification_announcer,
                    availability=channel_availability,
                ),
            ),
            consumer=CHALLENGE_NOTIFICATION_CONSUMER,
            event_types=CHALLENGE_NOTIFICATION_EVENTS,
        )
    )
    # A64-027.2. The analytics projector — the ninth consumer, and the first
    # that subscribes to events from five different modules at once. Its own
    # `processed_event` partition like every other: `game.match_completed`
    # now has five subscribers and none may mark another's work done.
    handlers.append(
        SessionScopedNotificationHandler(
            session_factory=db.session_factory,
            dispatcher_factory=lambda session: _analytics_projector_for(session, clock, settings),
            consumer=ANALYTICS_CONSUMER,
            event_types=frozenset(ANALYTICS_PROJECTIONS),
        )
    )
    if settings.matchmaking.realtime_delivery_enabled:
        handlers.append(
            SessionScopedNotificationHandler(
                session_factory=db.session_factory,
                dispatcher_factory=lambda session: _pending_match_notifier_for(
                    session, redis_pools, settings, clock, sink=pending_match_sink
                ),
                consumer=PENDING_MATCH_CONSUMER,
                event_types=PENDING_MATCH_EVENTS,
            )
        )
    else:
        # `INFO`: with the push off, `GET /matchmaking/matches/pending`
        # still answers and a client falls back to polling (§5). What is
        # lost is latency, not correctness.
        logger.info("pending_match_delivery_disabled", extra={"reason": "configuration"})

    return OutboxWorker(
        session_factory=db.session_factory,
        metrics=_metrics(),
        # A64-015.6 §5. Per-consumer budgets, so a slow sibling fails its own
        # slice rather than delaying everybody else's tick. The numbers are
        # ordered by what each consumer *does*, not by importance:
        #
        #   the timeline    one indexed insert per entry, no network
        #   the policy      queue writes, still local
        #   notifications   renders profiles, reads the social graph
        #   realtime        the only one that will talk to a socket (AD-09),
        #                   and the reason this exists at all
        #
        # Every one is a runaway guard rather than a latency target: a
        # consumer that exceeds these is stuck, not slow.
        policies=ConsumerPolicies.of(
            [
                ConsumerPolicy(TIMELINE_CONSUMER, timeout_seconds=10.0),
                ConsumerPolicy(ACCEPTANCE_FAILURE_CONSUMER, timeout_seconds=15.0),
                ConsumerPolicy(CONSUMER_NAME, timeout_seconds=20.0),
                ConsumerPolicy(PENDING_MATCH_CONSUMER, timeout_seconds=10.0),
                # The most expensive consumer on this list, and the budget
                # says so rather than hiding it: one completion can advance a
                # winner, publish the next round and create every match in
                # it — several transactions and a call into `game` — where
                # every other entry here is one or two indexed writes.
                ConsumerPolicy(TOURNAMENT_CONSUMER, timeout_seconds=30.0),
            ]
        ),
        # One handler today. The list is the extension point: a second
        # consumer — moderation, audit, statistics — is an entry here and
        # its own `processed_event` partition, with nothing above it
        # changing.
        handlers=handlers,
        settings=settings.outbox,
        clock=clock,
    )


def _clock_adjudication_for(
    session: AsyncSession, redis_pools: RedisPools, settings: Settings, clock: SystemClock
) -> ClockAdjudicationService:
    """One adjudication pass's graph — AD-21.

    The deadline store is on the **`live`** role beside the position it is
    about; a deadline on an evictable instance would make the eviction
    policy a way for a game to stop flagging.
    """
    return ClockAdjudicationService(
        matches=SqlAlchemyMatchRecordRepository(session),
        deadlines=RedisClockDeadlineStore(redis_pools.live),
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        batch_size=settings.game.clock_batch_size,
    )


def build_presence_sweeper(
    db: DatabaseSessionManager, redis_pools: RedisPools, settings: Settings
) -> PresenceSweeperWorker | None:
    """The presence sweeper for this process, or `None` if it does not run one.

    Closes the gap A64-013.7 recorded: a player whose window expires
    unobserved produces no `PresenceOffline`, so friends see them online
    until they come back and leave again. See `PresenceSweeper`.

    Assembled here for the same reason the relay's handler is: it composes
    `users`' presence adapter with the platform outbox, and composing across
    that line is the composition root's job.

    The **same `RedisPresenceProvider` instance** satisfies the roster and the
    reader, which is not incidental — the sweeper's re-check ("is this player
    back?") is only meaningful if it reads the store the roster describes.
    """
    if not settings.presence.sweeper_enabled:
        return None

    clock = SystemClock()

    def sweeper_for(session: AsyncSession) -> PresenceSweeper:
        presence = _presence_adapter(redis_pools, settings, clock)
        return PresenceSweeper(
            roster=presence,
            presence=presence,
            events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
            unit_of_work=SessionUnitOfWork(session),
            clock=clock,
            batch_size=settings.presence.sweep_batch_size,
        )

    return PresenceSweeperWorker(
        session_factory=db.session_factory,
        sweeper_factory=sweeper_for,
        interval_seconds=settings.presence.sweep_interval_seconds,
    )


def _rating_consumer_for(session: AsyncSession, clock: SystemClock) -> MatchCompletionConsumer:
    """The rating consumer over one session — A64-017.6.

    Named concretely here, which is what a composition root is for: the
    consumer holds `MatchRatingService`, which holds a repository, a
    publisher and a unit of work over the **same** session — so the two
    adjustments, the two rating rows and both `rating.updated` outbox rows
    are one transaction (§4).
    """
    return MatchCompletionConsumer(
        MatchRatingService(
            ratings=SqlAlchemyPlayerRatingRepository(session),
            events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
            unit_of_work=SessionUnitOfWork(session),
            clock=clock,
        )
    )


def _analytics_projector_for(
    session: AsyncSession, clock: SystemClock, settings: Settings
) -> AnalyticsProjector:
    """The analytics consumer over one session — A64-027.2 §15.

    The store, the subject directory and the unit of work all hold the
    **same** session, so a batch's subject resolutions and its event inserts
    are one transaction. A subject created for events that were then lost
    would leave a row in the one table erasure operates on for somebody with
    no history — harmless, and still wrong.
    """
    return AnalyticsProjector(
        store=SqlAlchemyAnalyticsEventStore(session),
        subjects=SqlAlchemySubjectDirectory(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        environment=current_environment(),
        metrics=_metrics(),
    )


def _statistics_consumer_for(
    session: AsyncSession, clock: SystemClock
) -> StatisticsMatchCompletionConsumer:
    """The statistics consumer over one session — A64-020.5F §8, §13.

    Named concretely here, which is what a composition root is for: the
    consumer holds `MatchProjectionService`, which holds the repository and
    a unit of work over the **same** session — so both players' claims and
    both counter updates are one transaction per match (§5).
    """
    return StatisticsMatchCompletionConsumer(
        projections=MatchProjectionService(
            statistics=SqlAlchemyStatisticsRepository(session),
            unit_of_work=SessionUnitOfWork(session),
            clock=clock,
        ),
        metrics=_metrics(),
    )


def _tournament_consumer_for(
    session: AsyncSession, settings: Settings, clock: SystemClock
) -> TournamentMatchCompletionConsumer:
    """The tournament consumer over one session — A64-019.5 §9.

    Named concretely here, which is what a composition root is for: this is
    the **only** place permitted to hand `tournament` a `game` command
    object. Its own `tournament-reaches-modules-through-public` contract
    covers its composition root as well, so `build_match_creation` — which
    names `game`'s `PersistentMatchCreation` — is called from here and
    passed in.

    Everything is built over the **same** session, so a bracket advancement,
    the matches it creates and the events both emit are one transaction each
    (AD-16) rather than a graph spread across connections.
    """
    events = OutboxEventPublisher(SqlAlchemyOutboxRepository(session))
    return build_match_completion_consumer(
        session,
        matches=build_match_creation(session, events=events, clock=clock),
        settings=settings.tournament,
        events=events,
        clock=clock,
    )


def _tournament_reconciliation_for(
    session: AsyncSession, settings: Settings, clock: SystemClock
) -> TournamentReconciliationService:
    """One reconciliation pass's graph — A64-019.5 §10.

    Two published `game` collaborators, both named here for the reason
    above: the command that creates a match, and the read that says what
    became of the ones this module already asked for.

    `GameOriginMatches` is `game`'s own adapter over its repository —
    assembled here rather than in `tournament`, which must not be able to
    name a `game` table.
    """
    events = OutboxEventPublisher(SqlAlchemyOutboxRepository(session))
    return build_tournament_reconciliation(
        session,
        matches=build_match_creation(session, events=events, clock=clock),
        origin_matches=GameOriginMatches(SqlAlchemyMatchRecordRepository(session)),
        settings=settings.tournament,
        events=events,
        clock=clock,
    )


def _tournament_no_show_for(
    session: AsyncSession, settings: Settings, clock: SystemClock
) -> TournamentNoShowService:
    """One no-show pass's graph — A64-019.5H §6e.

    Tournament matches are system-activated, so `game`'s acceptance expiry
    never claims one and nothing else would ever end a fixture nobody turned
    up for. This is what does, and it reads `game`'s authoritative state
    through the same published reader the reconciler uses — a real result
    always beats a lapsed deadline.
    """
    events = OutboxEventPublisher(SqlAlchemyOutboxRepository(session))
    return build_no_show_service(
        session,
        matches=build_match_creation(session, events=events, clock=clock),
        origin_matches=GameOriginMatches(SqlAlchemyMatchRecordRepository(session)),
        # A64-025.13A §36. Assembled here for the reason `GameOriginMatches`
        # is: it is `game`'s own adapter over `game`'s repository, and
        # `tournament` must not be able to name either.
        match_abort=PersistentMatchAbort(
            matches=SqlAlchemyMatchRecordRepository(session),
            events=events,
            unit_of_work=SessionUnitOfWork(session),
            clock=clock,
        ),
        settings=settings.tournament,
        events=events,
        clock=clock,
    )


def build_task_schedulers(
    db: DatabaseSessionManager, redis_pools: RedisPools, settings: Settings
) -> list[PeriodicTaskScheduler]:
    """This process's periodic background work — A64-014.1 onwards.

    Five jobs, all dispatched through `InlineTaskDispatcher` rather than
    called directly:

        platform.outbox.prune          the retention horizon A64-013.7
                                       shipped without
        matchmaking.queue.expire       the background half of `expires_at`
        matchmaking.queue.pair         one pool's pairing scan (A64-015.3)
        matchmaking.pairing.reconcile  the recovery A64-015.3 left to a
                                       human (A64-015.4)
        matchmaking.queue.prune        the queue history horizon A64-014.1
                                       shipped without (A64-015.5)

    Assembled here rather than in either owner because building a
    `QueueService` means naming a repository, a rating provider, a presence
    adapter, a publisher and a unit of work — composing across module lines
    is the composition root's job (BR-6 forbids a *module* reaching for the
    container, not the root wiring modules together).

    ## Why a dispatcher sits between the schedule and the work

    AD-17's claim is that moving to Celery replaces "only the dispatch
    adapter". Until A64-014.1 nothing on the platform dispatched anything,
    so the claim was untestable. With this shape, the migration is: swap
    `InlineTaskDispatcher` for a Celery one and these schedulers for beat
    entries. `OutboxRetentionTask` and `QueueExpiryTask` become task bodies
    unchanged, and neither `OutboxPruner` nor `QueueService` is touched.

    Each job is its own scheduler rather than a list on one, because they
    have different intervals and different SLO classes (AD-20) — a slow
    prune must not be able to delay an expiry sweep, which is precisely the
    interference separate queues exist to prevent.

    Returns an empty list when both are switched off, which is the intended
    shape of an API tier: the same image, with the maintenance work running
    on a worker tier instead.
    """
    clock = SystemClock()
    handlers: list[TaskHandler] = []
    # A64-027.2 §47's analytics retention, registered where the thing that
    # calls it lives — A64-028.4 §18.
    #
    # It was appended to `build_outbox_worker`'s list instead, which broke
    # the relay *and* left this unrouteable: `build_task_schedulers` below
    # schedules `analytics_prune_request` every six hours, and with no
    # handler answering to that name the dispatcher logs `task_unroutable`
    # and raises. So the 400-day retention this task exists for had never
    # run once.
    handlers.append(
        AnalyticsRetentionTask(
            session_factory=db.session_factory,
            clock=clock,
            metrics=_metrics(),
        )
    )
    schedulers: list[PeriodicTaskScheduler] = []

    # A64-021.5. Built once for this process rather than per pass: the
    # transport holds no session and the availability is a configuration
    # reading, and constructing either per pass would put the console
    # provider's production guard on a timer instead of at boot.
    email_provider = build_email_provider(settings.environment, settings.email)
    # A64-021.6. Same reasoning, and one thing more: `build_vapid_keys`
    # parses the pair, so a malformed or mismatched one is a **boot**
    # failure rather than a delivery failure on the first notification of
    # the day — see `PushSettings` on why fixing it later does not repair
    # the subscriptions created in between.
    push_provider = build_push_provider(build_vapid_keys(settings.push))
    channel_availability = channel_availability_for(settings)

    if settings.outbox.retention_enabled:
        handlers.append(
            OutboxRetentionTask(
                session_factory=db.session_factory,
                policy=retention_policy(
                    published_retention_days=settings.outbox.retention_days,
                    ledger_retention_days=settings.outbox.ledger_retention_days,
                    batch_size=settings.outbox.prune_batch_size,
                    max_batches=settings.outbox.prune_max_batches,
                ),
                clock=clock,
            )
        )
    else:
        # `WARNING`, not `INFO`, and it is the one switch here that deserves
        # it: with retention off the outbox grows without bound, and the
        # symptom arrives weeks later as a relay whose index no longer fits
        # in cache. An operator turning it off during an investigation
        # should see the reminder on every restart until they turn it back
        # on.
        logger.warning("outbox_retention_disabled", extra={"reason": "configuration"})

    if settings.matchmaking.expiry_enabled:
        handlers.append(
            QueueExpiryTask(
                session_factory=db.session_factory,
                service_factory=lambda session: _queue_service_for(
                    session, redis_pools, settings, clock
                ),
                batch_size=settings.matchmaking.expiry_batch_size,
            )
        )
    else:
        logger.info("queue_expiry_disabled", extra={"reason": "configuration"})

    if settings.matchmaking.pairing_enabled:
        handlers.append(
            PairingTask(
                session_factory=db.session_factory,
                service_factory=lambda session: _pairing_service_for(session, settings, clock),
            )
        )
    else:
        # `INFO`, not `WARNING`: this is a per-process switch and the
        # intended deployment has it off on the API tier. An operator seeing
        # it there is being told the expected state.
        logger.info("queue_pairing_disabled", extra={"reason": "configuration"})

    if settings.matchmaking.reconciliation_enabled:
        handlers.append(
            PairingReconciliationTask(
                session_factory=db.session_factory,
                service_factory=lambda session: _reconciliation_service_for(
                    session, redis_pools, settings, clock
                ),
            )
        )
    elif settings.matchmaking.pairing_enabled:
        # `WARNING`, and the pairing of the two flags is why: a process that
        # creates pairings and does not reconcile them is one whose crashed
        # scans strand two players until their ordinary ten-minute window
        # closes. Off *without* pairing is an ordinary API tier and is
        # unremarkable; off *with* it is a tier that can break things it
        # cannot fix.
        logger.warning(
            "pairing_reconciliation_disabled",
            extra={"reason": "configuration", "pairing_enabled": True},
        )
    else:
        logger.info("pairing_reconciliation_disabled", extra={"reason": "configuration"})

    if settings.matchmaking.retention_enabled:
        handlers.append(
            QueueRetentionTask(
                session_factory=db.session_factory,
                service_factory=lambda session: _queue_retention_for(session, settings, clock),
            )
        )
    else:
        # `WARNING`, like the outbox's own retention switch and for the same
        # reason: with it off, `queue_ticket`, `queue_cooldown` and the
        # abandoned half of `game.match` all grow without bound, and the
        # symptom arrives weeks later as a queue index that no longer fits
        # in cache.
        logger.warning("queue_retention_disabled", extra={"reason": "configuration"})

    # A64-022.6 §2. The friend challenge expiry sweep — the job that finally
    # writes down a transition the read predicates have been assuming since
    # A64-022.1. Its own switch, like every sweep here, so one tier runs it
    # and the API tier does not.
    if settings.matchmaking.challenge_expiry_enabled:
        handlers.append(
            ChallengeExpiryTask(
                session_factory=db.session_factory,
                service_factory=lambda session: _challenge_expiry_for(session, settings, clock),
            )
        )
    else:
        # `INFO`, not `WARNING`, and the difference from the retention
        # switch above is what stops happening. Retention off means a table
        # grows without bound; this off means challenges stay `pending` past
        # their window — invisible to every read and unanswerable either
        # way. A loss of record, not of rule (§2).
        logger.info("challenge_expiry_disabled", extra={"reason": "configuration"})

    # A64-016.5 §6, AD-21. The clock is adjudicated by a worker against
    # Redis rather than by in-process timers, because a timer lives on one
    # node and a node that is deployed takes every timer it held with it —
    # and those matches then never flag, they hang.
    if settings.game.clock_enabled:
        handlers.append(
            ClockAdjudicationTask(
                session_factory=db.session_factory,
                service_factory=lambda session: _clock_adjudication_for(
                    session, redis_pools, settings, clock
                ),
            )
        )
        # A64-028.4, P3-4 reclassified. The queue this adjudicator reads is
        # a Redis sorted set with no durable backing, and
        # `ClockAdjudicationService` has said since A64-018 that a lost
        # deadline means "the match stops flagging … for a game nobody is
        # moving in it stays open". A64-028.3 proved the set does not
        # survive a Redis loss and filed the missing backstop as a P3 about
        # growth; the growth is the small half. This is the sweep that
        # docstring names, re-deriving every active match's deadline from
        # the columns the move committed.
        handlers.append(
            ClockDeadlineReconciliationTask(
                session_factory=db.session_factory,
                deadlines=RedisClockDeadlineStore(redis_pools.live),
                clock=clock,
                metrics=_metrics(),
            )
        )
    else:
        # `WARNING`: with it off, moves still charge time and the move log
        # still records it, and **no match ever flags**. That is a game
        # nobody can lose on time, which is a silent product change rather
        # than a degraded feature.
        logger.warning("clock_adjudication_disabled", extra={"reason": "configuration"})

    # A64-016.8, closing A64-016.5 §9. Until this existed, a frame published
    # for another node was written to that node's stream and read by nobody:
    # the transport had a writer and a reader and no loop between them, so a
    # multi-node deployment lost every cross-node frame while reporting them
    # delivered. See `app/gateway/forwarding.py`.
    if settings.gateway.forwarding_enabled:
        handlers.append(
            GatewayForwardingTask(
                GatewayForwarder(
                    bus=get_gateway_bus_for(redis_pools, settings.gateway),
                    # The **process-wide** registry, shared with the request
                    # path — see `get_local_sockets` on why a second instance
                    # would be a forwarder delivering into an empty map.
                    sockets=get_local_sockets(),
                    metrics=_metrics(),
                    node_id=resolve_node_id(settings.gateway),
                    batch_size=settings.gateway.forwarding_batch_size,
                )
            )
        )
    else:
        # `WARNING`: with it off, a node publishes to its peers and reads
        # nothing back, which on more than one node is a fleet whose players
        # can only see opponents who happen to have landed on the same
        # process.
        logger.warning("gateway_forwarding_disabled", extra={"reason": "configuration"})

    # A64-019.2 §2, §9. `registration_deadline` is a promise to players:
    # registration closes when it is reached without an operator being
    # awake. A task rather than a timer, for AD-21's reason — a timer lives
    # on one node and a deploy takes it with them, and those tournaments
    # then never close, they hang.
    #
    # Idempotent by predicate: the claim is "open **and** overdue", so a
    # tournament already closed does not match and a second worker finds
    # nothing. There is no ledger to keep.
    handlers.append(
        TournamentDeadlineTask(
            session_factory=db.session_factory,
            service_factory=lambda session: build_deadline_service(
                session,
                events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
                clock=clock,
            ),
        )
    )

    # A64-019.5 §10. Creating a tournament match is two writes that cannot
    # share a transaction (BE-05): `game` commits the match, then
    # `tournament` records the attempt. A worker that dies between them
    # leaves a bracket waiting on a match it does not know exists — this is
    # what finds it.
    #
    # Always registered, like the deadline sweep: the drift it repairs is
    # caused by a process dying, so a deployment that ran tournaments
    # without it would have no way back from its own crashes.
    handlers.append(
        TournamentReconciliationTask(
            session_factory=db.session_factory,
            service_factory=lambda session: _tournament_reconciliation_for(
                session, settings, clock
            ),
        )
    )

    # A64-019.5H §6e. A tournament match is **system-activated** — nobody is
    # asked to accept a fixture they entered a tournament to play — so
    # `game`'s acceptance expiry never claims one, and without this a
    # bracket would wait forever on a player who never arrived.
    #
    # Always registered, like the two sweeps above: a deployment running
    # tournaments without it is one whose rounds stall on the first
    # absentee.
    handlers.append(
        TournamentNoShowTask(
            session_factory=db.session_factory,
            service_factory=lambda session: _tournament_no_show_for(session, settings, clock),
        )
    )

    # A64-015.6 §6. Always registered — there is no switch, because the
    # accumulator is filled by services that are always wired and a process
    # that never drained it would hold counters forever and report none.
    # A64-021.5 §24. The email worker, and it is registered only when the
    # channel is on — a handler that claimed deliveries in a process that
    # cannot send them would mark every one `skipped_channel_unavailable`
    # and quietly drain the queue.
    #
    # `NOTIFICATION_EMAIL_ENABLED` is off by default: the phase built the
    # channel and deliberately chose no vendor, so a deployment that has not
    # configured one runs without this task and reports email unavailable in
    # Settings. See `NotificationEmailSettings`.
    if email_channel_available(settings):
        handlers.append(
            NotificationEmailDeliveryTask(
                session_factory=db.session_factory,
                service_factory=lambda session: build_email_delivery_service(
                    session,
                    provider=email_provider,
                    metrics=_metrics(),
                    clock=clock,
                    availability=channel_availability,
                    settings=settings.notification_email,
                    public_url=settings.app.public_url,
                ),
            )
        )
    else:
        # `INFO`: an unconfigured channel is a deployment state, not a
        # fault. What makes it safe is that Settings agrees — the preference
        # API reports email unavailable from the same value.
        logger.info("notification_email_disabled", extra={"reason": "configuration"})

    # A64-027A §19. Unconditional, unlike the two above it: in-app delivery
    # has no provider to be missing, so a process that can serve the admin
    # API can always finish the broadcasts that API queues.
    handlers.append(
        NotificationBroadcastTask(
            session_factory=db.session_factory,
            # No announcer, like the email and push services beside it.
            # The process-wide `GatewayNotificationSink` is built in the
            # request-serving factory and holds the fleet fan-out; a second
            # instance here would be a second transport. A broadcast
            # therefore arrives on a client's next refetch rather than as a
            # live frame — which A64-021.2's own note allows, since the
            # list and the unread badge refetch on focus regardless.
            service_factory=lambda session: build_broadcast_expander(
                session,
                clock=clock,
            ),
        )
    )

    # A64-021.6 §18. Registered only when this process holds a VAPID key
    # pair, for the same reason as email: a handler claiming deliveries in a
    # process that cannot send them would mark every one
    # `skipped_channel_unavailable` and quietly drain the queue.
    if push_provider is not None:
        handlers.append(
            NotificationPushDeliveryTask(
                session_factory=db.session_factory,
                service_factory=lambda session: build_push_delivery_service(
                    session,
                    provider=push_provider,
                    metrics=_metrics(),
                    clock=clock,
                    availability=channel_availability,
                    settings=settings.push,
                ),
            )
        )
    else:
        # `INFO`: an unconfigured channel is a deployment state, not a
        # fault. What makes it safe is that Settings agrees — the preference
        # API reports push unavailable from the same value.
        logger.info("notification_push_disabled", extra={"reason": "configuration"})

    handlers.append(MetricsFlushTask(metrics=_metrics()))

    if not handlers:
        return schedulers

    dispatcher = InlineTaskDispatcher(handlers)
    logger.info("task_dispatcher_ready", extra={"tasks": sorted(dispatcher.registered)})

    if settings.outbox.retention_enabled:
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=prune_request(),
                interval_seconds=settings.outbox.prune_interval_seconds,
            )
        )
    if settings.analytics.retention_enabled:
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=analytics_prune_request(),
                interval_seconds=settings.analytics.prune_interval_seconds,
            )
        )
    if settings.matchmaking.expiry_enabled:
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=expiry_request(),
                interval_seconds=settings.matchmaking.expiry_interval_seconds,
            )
        )
    if settings.game.clock_enabled:
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=adjudication_request(),
                interval_seconds=settings.game.clock_interval_seconds,
            )
        )
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=clock_reconcile_request(),
                interval_seconds=settings.game.clock_reconcile_interval_seconds,
            )
        )
    if email_channel_available(settings):
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=email_delivery_request(),
                interval_seconds=settings.notification_email.poll_interval_seconds,
            )
        )
    if push_provider is not None:
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=push_delivery_request(),
                interval_seconds=settings.push.poll_interval_seconds,
            )
        )
    schedulers.append(
        PeriodicTaskScheduler(
            dispatcher=dispatcher,
            request=broadcast_delivery_request(),
            interval_seconds=settings.broadcast.poll_interval_seconds,
        )
    )
    if settings.gateway.forwarding_enabled:
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=forwarding_request(),
                interval_seconds=settings.gateway.forwarding_interval_seconds,
            )
        )
    if settings.matchmaking.pairing_enabled:
        # **One scheduler per pool**, because a pairing scan is per pool
        # (A64-015.3 §1) and `PeriodicTaskScheduler` carries one request.
        # Fourteen today — see `every_pool` on why enumerating them is
        # right at this size and what replaces it when it is not.
        schedulers.extend(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=pairing_request(pool),
                interval_seconds=settings.matchmaking.pairing_interval_seconds,
            )
            for pool in every_pool()
        )
    schedulers.append(
        PeriodicTaskScheduler(
            dispatcher=dispatcher,
            request=tournament_deadline_request(),
            # A minute: a registration that closes a few seconds late costs
            # nobody a game, and a tighter tick would be a sweep that is
            # almost always empty.
            interval_seconds=60.0,
        )
    )
    schedulers.append(
        PeriodicTaskScheduler(
            dispatcher=dispatcher,
            request=tournament_reconciliation_request(),
            # Five minutes. The drift it repairs is created by a process
            # dying, which is rare, and every one of its repairs is also
            # reached by the ordinary path — the outbox redelivers, and a
            # start is idempotent. A tighter tick would be a sweep that is
            # almost always empty and a claim that contends with the
            # consumer for the same rows.
            interval_seconds=300.0,
        )
    )
    schedulers.append(
        PeriodicTaskScheduler(
            dispatcher=dispatcher,
            request=tournament_no_show_request(),
            # The **resolution of the adjudication**: a no-show is decided
            # within this of its deadline, and a round waiting on one waits
            # this much longer than it has to. `TournamentSettings` checks it
            # stays well below the deadline it enforces.
            interval_seconds=settings.tournament.no_show_interval_seconds,
        )
    )
    schedulers.append(
        PeriodicTaskScheduler(
            dispatcher=dispatcher,
            request=flush_request(),
            interval_seconds=settings.app.metrics_flush_interval_seconds,
        )
    )
    if settings.matchmaking.retention_enabled:
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=queue_retention_request(),
                interval_seconds=settings.matchmaking.retention_interval_seconds,
            )
        )
    if settings.matchmaking.challenge_expiry_enabled:
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=challenge_expiry_request(),
                interval_seconds=settings.matchmaking.challenge_expiry_interval_seconds,
            )
        )
    if settings.matchmaking.reconciliation_enabled:
        # **One scheduler, not one per pool.** A stranded reservation is a
        # stranded reservation whatever pool produced it, and the claim that
        # finds them is pool-blind — the same shape the expiry sweep has.
        schedulers.append(
            PeriodicTaskScheduler(
                dispatcher=dispatcher,
                request=reconciliation_request(),
                interval_seconds=settings.matchmaking.reconciliation_interval_seconds,
            )
        )
    return schedulers


def _queue_service_for(
    session: AsyncSession, redis_pools: RedisPools, settings: Settings, clock: SystemClock
) -> QueueService:
    """A `QueueService` over one task run's session.

    Goes through `matchmaking`'s own `build_queue_service` rather than
    assembling the graph a second time here, so the background path and the
    HTTP path are provably the same object graph — a hand-built copy would
    drift the first time either gained a collaborator.

    The publisher is built over this same session, which is what puts each
    `QueueTicketExpired` row in the transaction that resolves its ticket
    (AD-16).
    """
    return build_queue_service(
        session,
        eligibility=build_eligibility_policy(
            session, _presence_adapter(redis_pools, settings, clock), clock=clock
        ),
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
        settings=settings.matchmaking,
        clock=clock,
    )


def _pairing_service_for(
    session: AsyncSession, settings: Settings, clock: SystemClock
) -> PairingService:
    """A `PairingService` over one scan's session — A64-015.3, A64-015.4.

    Goes through `matchmaking`'s own `build_pairing_service` for the same
    reason `_queue_service_for` does: one object graph, defined once, so a
    hand-built copy here cannot drift.

    The three collaborators this root chooses are the three that cross a
    module boundary — `friends`' block read, `game`'s command port and
    `game`'s rematch guard — and as of A64-015.4 all three are real. The
    publisher is built over this **same session**, which is what puts
    `game.match_created` in the match's own transaction and `PlayersPaired`
    in the tickets' (AD-16); they are two transactions because BE-05
    forbids collapsing them, and `_reconciliation_service_for` below is what
    closes the window between.
    """
    return build_pairing_service(
        session,
        exclusions=build_pairing_exclusions(session),
        opponents=build_recent_opponents(session),
        matches=build_match_creation(
            session,
            events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
            clock=clock,
        ),
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
        settings=settings.matchmaking,
        clock=clock,
        metrics=_metrics(),
    )


def _reconciliation_service_for(
    session: AsyncSession, redis_pools: RedisPools, settings: Settings, clock: SystemClock
) -> PairingReconciliationService:
    """A `PairingReconciliationService` over one pass's session —
    A64-015.4 §9.

    The two `game` collaborators are the published *reads* and the
    published *sweep*, never a repository: this module recovers its own
    tickets and asks `game` to expire its own matches, and neither reaches
    into the other's table.

    The acceptance service is built here rather than shared with the HTTP
    path deliberately — it holds a session, and a session must not outlive
    the unit of work it serves. What is shared is the *factory*, so the
    route's graph and the task's are provably the same objects over
    different sessions.
    """
    return build_reconciliation_service(
        session,
        settlements=build_pairing_settlements(session),
        acceptance=build_match_acceptance(
            session,
            events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
            clock=clock,
            metrics=_metrics(),
            deadlines=RedisClockDeadlineStore(redis_pools.live),
        ),
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
        settings=settings.matchmaking,
        clock=clock,
        metrics=_metrics(),
    )


def _match_outcome_for(
    session: AsyncSession, redis_pools: RedisPools, settings: Settings, clock: SystemClock
) -> MatchOutcomeService:
    """The acceptance-failure policy over one relay tick's session —
    A64-015.5 §1.

    Composes `matchmaking`'s queue use cases with its cooldown store, and
    takes the *same* eligibility policy the HTTP join path uses — which is
    what makes "a player in cooldown is not requeued" true without a second
    rule. See `QueueService.requeue`.
    """
    return build_match_outcome_service(
        session,
        eligibility=build_eligibility_policy(
            session, _presence_adapter(redis_pools, settings, clock), clock=clock
        ),
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
        settings=settings.matchmaking,
        clock=clock,
        metrics=_metrics(),
    )


def _pending_match_notifier_for(
    session: AsyncSession,
    redis_pools: RedisPools,
    settings: Settings,
    clock: SystemClock,
    *,
    sink: PendingMatchSink,
) -> PendingMatchNotifier:
    """The realtime pending-match consumer over one relay tick's session —
    A64-015.5 §4.

    The sink is passed in rather than built here, because it is the one
    collaborator in this graph that is **process-wide**: a socket gateway
    holds connections, and rebuilding it per relay tick would be rebuilding
    the transport.
    """
    return build_pending_match_notifier(
        session,
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
        sink=sink,
        clock=clock,
        metrics=_metrics(),
        deadlines=RedisClockDeadlineStore(redis_pools.live),
    )


def _queue_retention_for(
    session: AsyncSession, settings: Settings, clock: SystemClock
) -> QueueRetentionService:
    """One retention run's object graph — A64-015.5 §8."""
    return build_queue_retention_service(
        session, settings=settings.matchmaking, clock=clock, metrics=_metrics()
    )


def _challenge_expiry_for(
    session: AsyncSession, settings: Settings, clock: SystemClock
) -> ChallengeExpiryService:
    """One friend challenge expiry sweep's object graph — A64-022.6 §2."""
    return build_challenge_expiry_service(
        session,
        settings=settings.matchmaking,
        clock=clock,
        metrics=_metrics(),
        # The outbox over the same session as the repository, so the
        # transition and the event announcing it commit together (AD-16).
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
    )


def _metrics() -> AggregatingMetrics:
    """The process-wide metrics recorder — A64-015.6 §6 and §10.

    Delegates to `platform.metrics.process_metrics` rather than constructing
    one, which is the point: the request path reaches the same accessor
    through `matchmaking`'s `get_metrics`, so both halves of the process
    count into one accumulator and `MetricsFlushTask` drains all of it. See
    that module for what the two-recorder arrangement was losing.

    It stays a named function here because every wiring site below reads
    `metrics=_metrics()` and the indirection is the composition root's own
    vocabulary.
    """
    return process_metrics()


def _presence_adapter(
    redis_pools: RedisPools, settings: Settings, clock: SystemClock
) -> RedisPresenceProvider | NoPresenceProvider:
    """The presence store this process talks to.

    Branches on `PRESENCE_ENABLED` exactly as every other presence factory
    does. With presence off the sweeper reads an always-empty roster and
    ticks harmlessly — which is why the sweeper needs no kill switch for
    *that* condition, only for "does this process run it".
    """
    if not settings.presence.enabled:
        return NoPresenceProvider()
    return RedisPresenceProvider(redis_pools.cache, settings=settings.presence, clock=clock)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown, in one place and in a defined order."""
    # DI-06: settings are validated here, before the process accepts a
    # single request. A missing or malformed value raises out of
    # get_settings() and the process never reaches `yield` — a visible,
    # automatically-rolled-back deploy failure, not a 3am outage.
    settings = get_settings()

    configure_logging(
        level=settings.app.log_level,
        environment=settings.environment,
        format_override=settings.app.log_format,
    )
    logger.info("startup_begin", extra={"environment": settings.environment.value})
    # A64-021.5. Which configuration file this process actually read, and
    # whether it found one. Silence here is what let a `.env` sit unread
    # beside a platform behaving exactly as if it had no configuration —
    # because it had none. The line names the file and, when a
    # differently-named one is present, says so.
    logger.info("configuration_source", extra={"env_file": describe_env_file(settings.environment)})

    db = DatabaseSessionManager(settings.postgres)
    redis_pools = create_redis_pools(settings.redis)

    # A64-011.8. Built once per process rather than per request: the
    # constructor computes the SHA of its Lua script so every check can use
    # EVALSHA, and that work is wasted if it is redone on the hottest
    # unauthenticated path on the platform. See `app/api/deps.py`.
    #
    # Uses the `limits` Redis role, never `cache` — a rate limit counter
    # evicted under memory pressure is a limit that disappears during
    # exactly the traffic spike it exists for (`RedisSettings`).
    rate_limiter = RedisRateLimiter(
        redis_pools.limits,
        settings=settings.rate_limit,
        clock=SystemClock(),
        metrics=_metrics(),
    )

    if not settings.rate_limit.enabled:
        # Loud, and at WARNING, because the kill switch is invisible
        # otherwise: an endpoint with no limiter looks exactly like an
        # endpoint with a working one until somebody attacks it.
        logger.warning(
            "rate_limiting_disabled",
            extra={"environment": settings.environment.value},
        )

    # Everything a route reaches via app/api/deps.py lives here. This is
    # the HTTP-native half of the bridge described in deps.py's docstring;
    # a future non-HTTP entrypoint resolves the same singletons through
    # app.core.di.Container instead, since it has no app.state.
    app.state.settings = settings
    app.state.db = db
    app.state.redis_pools = redis_pools
    app.state.rate_limiter = rate_limiter

    # A64-013.7. The outbox relay, started **after** everything it needs is
    # on `app.state` and stopped **before** any of it is torn down — a tick
    # holding a session while the engine closes underneath it is the shape
    # that turns every shutdown into a confusing error.
    #
    # Started in this process only when `OUTBOX_WORKER_ENABLED` says so. The
    # intended deployment is one API tier with it off and one small worker
    # tier with it on, running the same image; the default is `true` because
    # a single-node development environment has no second tier and a feature
    # that silently never delivers is worse than one that competes for the
    # event loop. See `OutboxWorker`.
    outbox_worker = build_outbox_worker(db, redis_pools, settings)
    if outbox_worker is not None:
        await outbox_worker.start()
    else:
        logger.info("outbox_worker_disabled", extra={"reason": "configuration"})
    app.state.outbox_worker = outbox_worker

    # A64-013.8. The presence sweeper, started beside the relay and stopped
    # before it: the sweeper *produces* events the relay consumes, so on the
    # way down the producer is quiesced first and the relay is given a final
    # tick's worth of time to drain what it already claimed.
    presence_sweeper = build_presence_sweeper(db, redis_pools, settings)
    if presence_sweeper is not None:
        await presence_sweeper.start()
    else:
        logger.info("presence_sweeper_disabled", extra={"reason": "configuration"})
    app.state.presence_sweeper = presence_sweeper

    # A64-014.1. The periodic dispatched work — outbox retention and queue
    # expiry — started last and stopped first, for the same producer-before-
    # consumer reason the sweeper is: the expiry sweep *writes* outbox rows
    # the relay reads, so on the way down every producer is quiesced before
    # the relay is given its final tick.
    task_schedulers = build_task_schedulers(db, redis_pools, settings)
    for scheduler in task_schedulers:
        await scheduler.start()
    app.state.task_schedulers = task_schedulers

    # The loop's own health, measured inside the process that has to stay
    # responsive — A64-028.6 §4. A64-028.5A could report only the load
    # generator's lag and said so; that number says nothing about whether
    # the server's loop is blocked, which is the failure that makes an
    # asyncio service stop answering while every dependency it has is fine.
    loop_probe = EventLoopLagProbe(
        metrics=_metrics(),
        interval_seconds=settings.app.event_loop_probe_interval_seconds,
    )
    await loop_probe.start()

    logger.info("startup_complete")
    try:
        yield
    finally:
        logger.info("shutdown_begin")
        # First on the way down: it is the only background task whose
        # output is about the shutdown itself, and a lag sample taken while
        # the recorder's sink is closing is noise.
        await loop_probe.stop()
        for scheduler in task_schedulers:
            await scheduler.stop()
        if presence_sweeper is not None:
            await presence_sweeper.stop()
        if outbox_worker is not None:
            await outbox_worker.stop()
        # A64-027.1. `_configure_storage`'s note said an S3 client would be
        # closed here "at the point it exists"; it exists now. Duck-typed
        # rather than `isinstance`, because closing is a property of holding
        # a connection pool and not of being one particular provider —
        # `LocalStorageProvider` holds a path and has nothing to release.
        storage = getattr(app.state, "storage", None)
        closer = getattr(storage, "aclose", None)
        if closer is not None:
            await closer()
        await redis_pools.aclose()
        await db.close()
        logger.info("shutdown_complete")


def _configure_storage(app: FastAPI, settings: Settings) -> None:
    """Builds the object-storage provider and, in development, serves it.

    **The only place on the platform that names a concrete provider.**
    Everything else — every service, every dependency, every route —
    takes `StorageProvider`. Adding S3, R2, MinIO or GCS is a branch here
    and nothing else, which is what makes A64-012.2's "without changing
    business logic" a structural claim rather than an intention.

    `LocalStorageProvider` refuses to construct in a production-like
    environment (objects would live on one node's disk and vanish on
    reschedule, and a second replica would serve nothing at all), so a
    deployed tier that never sets `STORAGE_PROVIDER` fails at startup
    rather than accepting uploads it is going to lose.

    ## Why this is in `create_app` and not `lifespan`

    Everything else on `app.state` is built in `lifespan`, because engines
    and connection pools need an ordered async teardown. Storage does not:
    the local provider holds a resolved path and nothing else.

    What decided it is the mount. `StaticFiles` has to be part of the
    route table, and an app that skipped `lifespan` — every contract test
    on this platform drives the app over `ASGITransport`, which does — would
    otherwise have a working upload endpoint and no way to fetch what it
    stored. Building both here keeps the app self-contained and makes
    "the URL actually serves" testable, which is exactly the property a
    storage abstraction is easiest to get subtly wrong about.

    A provider that eventually needs an async close — an S3 client — gets
    that wired into `lifespan` at the point it exists, alongside its
    construction.
    """
    # The one branch A64-012.2 predicted, and the only place on the platform
    # that names a concrete provider.
    storage: LocalStorageProvider | S3StorageProvider = (
        S3StorageProvider(settings.storage)
        if settings.storage.provider == "s3"
        else LocalStorageProvider(settings.storage, settings.environment)
    )
    app.state.storage = storage

    # Development only, and the guard is the provider's own type rather
    # than an environment check: `LocalStorageProvider` cannot exist in a
    # production-like tier, so neither can this mount.
    #
    # A deployed tier serves objects from the store, or from a CDN in front
    # of it — never through this process. Routing image bytes through
    # Python would put an ASGI worker on the hot path of every avatar
    # render, which is the thing object storage exists to avoid.
    if isinstance(storage, LocalStorageProvider):
        storage.root.mkdir(parents=True, exist_ok=True)
        app.mount(
            settings.storage.public_url_path,
            StaticFiles(directory=storage.root),
            name="media",
        )


def create_app() -> FastAPI:
    """Assembles middleware, exception handlers, and routers. Wires nothing
    a route handler could not otherwise reach through `app.state` or
    `Depends` — see dependency-injection.md DI-01.
    """
    app = FastAPI(
        title="Arena64 API",
        version="0.1.0",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )

    # Order is immaterial: A64-008 decoupled the two middlewares (see
    # common/middleware.py's docstring), so neither depends on the other
    # having already run.
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(RequestIdMiddleware)
    # Added last, so it is the **outermost** of the three: a request that
    # ends as an unhandled exception must be counted as one, and a
    # middleware inside `register_exception_handlers` would only ever see
    # the response the handler produced. A64-028.6 §3.
    in_flight = InFlight()
    app.add_middleware(HttpMetricsMiddleware, metrics=_metrics(), in_flight=in_flight)
    register_exception_handlers(app)

    settings = get_settings()
    _configure_storage(app, settings)
    _register_gauges(prometheus_metrics(), in_flight, process_backlog(), settings)

    # Unversioned, for load-balancer and orchestrator probes: a liveness
    # check must not sit behind API versioning that could itself fail to
    # resolve (app/api/v1/health.py's docstring).
    app.include_router(health_router)

    # The operator surface — A64-028.6 §5, §9. Unversioned for the same
    # reason as the probes, and each route present only when configuration
    # asks for it: a route that exists and refuses is still a signal that
    # there is something behind it.
    observability = settings.observability
    if observability.metrics_enabled:
        app.include_router(build_metrics_router(prometheus_metrics(), settings))
    if observability.drain_enabled:
        app.include_router(build_drain_route(settings))

    app.include_router(api_router, prefix=API_PREFIX)

    # A64-016.1. Unversioned in the path, like the health probe and for a
    # different reason: a WebSocket negotiates its protocol version *in
    # band* (`gateway.protocol.PROTOCOL_VERSION`), so a version in the URL
    # would pin a connection that lives for an hour to a number chosen at
    # connect time. See `app/gateway/router.py`.
    app.include_router(gateway_router)

    return app


def _register_gauges(
    exporter: PrometheusMetrics,
    in_flight: InFlight,
    outbox_backlog: BacklogSnapshot,
    settings: Settings,
) -> None:
    """Values the exporter reads at scrape time — A64-028.6 §3.

    A depth is a thing that *is*, not a thing that happened, and
    `platform/metrics/prometheus.py` explains why that gets its own
    mechanism rather than a counter somebody tries to keep in step by
    arithmetic. Registered here because the composition root is the only
    place that knows both the exporter and the objects holding the values.

    The outbox gauges are deliberately **not** here: they read the database,
    and a scrape must not open a session per series. They are published by
    the relay's own tick instead, which already has one open — see
    `OutboxBacklogTask`.
    """

    async def read_in_flight() -> dict[tuple[tuple[str, str], ...], float]:
        return {(): float(in_flight.count)}

    exporter.register_gauge(
        REQUESTS_IN_FLIGHT,
        "HTTP requests being served at this instant.",
        read_in_flight,
    )

    async def read_backlog() -> dict[tuple[tuple[str, str], ...], float]:
        backlog = outbox_backlog.value
        if backlog is None:
            # A process that has not ticked yet publishes nothing rather
            # than zero — "no backlog" is the reading an operator would
            # most regret trusting.
            return {}
        return {
            (("state", "retryable"),): float(backlog.retryable),
            (("state", "exhausted"),): float(backlog.exhausted),
        }

    async def read_oldest() -> dict[tuple[tuple[str, str], ...], float]:
        backlog = outbox_backlog.value
        return {} if backlog is None else {(): backlog.oldest_pending_age_seconds}

    async def read_draining() -> dict[tuple[tuple[str, str], ...], float]:
        # Published so the readiness alert can exclude a deploy: an
        # instance answering 503 because it was told to go away is the
        # deploy working, and paging somebody for it would train the
        # audience to ignore the channel.
        return {(): 1.0 if service_lifecycle().draining else 0.0}

    exporter.register_gauge(
        "service.draining", "1 while this instance is draining, 0 otherwise.", read_draining
    )

    async def read_backup_age() -> dict[tuple[tuple[str, str], ...], float]:
        destination = settings.observability.backup_destination
        if destination is None:
            # Absent rather than zero — see the setting's docstring. A
            # deployment where nothing can see the backups must fire
            # `BackupNeverSucceeded`, not report a fresh one.
            return {}
        status = backup_status.read(destination)
        if status.succeeded_at is None:
            return {}
        return {(): status.succeeded_at.timestamp()}

    exporter.register_gauge(
        "backup.last_success_timestamp_seconds",
        "Unix time of the last successful backup this process can see.",
        read_backup_age,
    )

    async def read_certificate_expiry() -> dict[tuple[tuple[str, str], ...], float]:
        path = settings.observability.certificate_path
        if path is None:
            return {}
        status = certificate_status.read(path)
        if status is None:
            # Absent rather than zero — a certificate that cannot be read is
            # a different incident from one that has expired, and zero would
            # report the second when the first is true.
            return {}
        return {(): status.not_after.timestamp()}

    exporter.register_gauge(
        "certificate.expiry_timestamp_seconds",
        "Unix time the TLS certificate this process can see stops being valid.",
        read_certificate_expiry,
    )

    exporter.register_gauge(BACKLOG, "Unpublished outbox entries, by state.", read_backlog)
    exporter.register_gauge(
        OLDEST_PENDING_AGE,
        "Age of the oldest retryable outbox entry, in seconds.",
        read_oldest,
    )
