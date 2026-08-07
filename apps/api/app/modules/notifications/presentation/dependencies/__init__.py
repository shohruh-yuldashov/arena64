"""The `Depends` bridge for `notifications` — DI-01.

**Three factories, and only two are `Depends`**, which is the shape a
module built around an outbox has:

    get_notification_service             resolved by this module's own routes
                                         — A64-021.1. The reader
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

from app.api.deps import ClockDep, DbSessionDep, PresenceSettingsDep, SettingsDep
from app.api.outbox_deps import EventPublisherDep
from app.config.settings import NotificationEmailSettings, PushSettings, Settings
from app.core.clock import Clock
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
from app.modules.notifications.application.ports import (
    DurableNotificationStore,
    EmailDeliveryRepository,
    NotificationAnnouncer,
    NotificationSink,
    PushDeliveryRepository,
)
from app.modules.notifications.application.services import (
    DurableNotificationWriter,
    GameNotificationDispatcher,
    NotificationPreferenceService,
    NotificationService,
    PreferenceDeliveryPolicy,
    PresenceNotificationService,
    SocialNotificationDispatcher,
    TournamentNotificationDispatcher,
)
from app.modules.notifications.application.services.email_delivery_service import (
    EmailDeliveryService,
)
from app.modules.notifications.application.services.push_delivery_service import (
    PushDeliveryService,
)
from app.modules.notifications.application.services.push_subscription_service import (
    PushSubscriptionService,
)
from app.modules.notifications.domain.preference import (
    IN_APP_ONLY,
    ChannelAvailability,
    DeliveryChannel,
)
from app.modules.notifications.infrastructure import NullNotificationAnnouncer
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyEmailDeliveryRepository,
    SqlAlchemyNotificationPreferenceRepository,
    SqlAlchemyNotificationRepository,
    SqlAlchemyPushDeliveryRepository,
    SqlAlchemyPushSubscriptionRepository,
)
from app.modules.notifications.presentation.email import TemplateEmailRenderer
from app.modules.profiles.application.services.profile_renderer import BatchProfileRenderer
from app.modules.tournament.public import TournamentNotificationReader
from app.modules.users.infrastructure.repositories.email_recipient_directory import (
    SqlAlchemyEmailRecipientDirectory,
)
from app.modules.users.presentation.dependencies import PresenceServiceDep
from app.platform.email import EmailProvider, can_deliver_email
from app.platform.metrics import MetricsRecorder
from app.platform.outbox import NoEventPublisher
from app.platform.push import PushProvider, can_deliver_push

logger = logging.getLogger(__name__)


def email_channel_available(settings: Settings) -> bool:
    """Whether this process can put a notification email in an inbox.

    **Two conditions, and one place that composes them.** A transport —
    `RESEND_API_KEY`, read through `platform.email.can_deliver_email`, the
    same value that decides which provider is built — and the operational
    kill switch on `NotificationEmailSettings`. Composing them here rather
    than at each of the four call sites is what stops a settings screen and a
    delivery worker disagreeing about whether email works, which is the
    failure §5 and §26 both name.
    """
    return settings.notification_email.enabled and can_deliver_email(settings.email)


def channel_availability_for(settings: Settings) -> ChannelAvailability:
    """What this **process** can deliver on — A64-021.5 §26.

    Derived from configuration rather than from a constant, and that is the
    whole point: whether email works is a fact about the transports this
    deployment wired, not about the build. A settings screen told otherwise
    would be offering a switch nothing honours.

    A plain function, not a `Depends`: the background composition needs the
    same value and has no request to resolve against.
    """
    channels = [DeliveryChannel.IN_APP]
    if email_channel_available(settings):
        channels.append(DeliveryChannel.EMAIL)
    # A64-021.6 §6. The same rule as email, asked of the value that decides
    # whether a message can be signed at all: a process holding a VAPID key
    # pair can push, one without cannot, and there is deliberately no second
    # boolean beside the keys that could disagree with them.
    if can_deliver_push(settings.push):
        channels.append(DeliveryChannel.PUSH)
    return ChannelAvailability.of(*channels)


def get_channel_availability(settings: SettingsDep) -> ChannelAvailability:
    """The same value, for a request."""
    return channel_availability_for(settings)


ChannelAvailabilityDep = Annotated[ChannelAvailability, Depends(get_channel_availability)]


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


def get_notification_service(session: DbSessionDep, clock: ClockDep) -> NotificationService:
    """The reader behind `/notifications` — A64-021.1 §15.

    Per request, over the request's session, like every other service on
    this platform. Holds one repository: reading and marking notifications
    needs nothing else, and a service that could reach a profile or a
    friendship would be one that could re-resolve a snapshot at read time.
    """
    return NotificationService(
        notifications=SqlAlchemyNotificationRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


NotificationServiceDep = Annotated[NotificationService, Depends(get_notification_service)]


def get_notification_preference_service(
    session: DbSessionDep, clock: ClockDep, availability: ChannelAvailabilityDep
) -> NotificationPreferenceService:
    """The settings screen's service — A64-021.3 §14.

    Per request, over the request's session. Holds only the preference
    repository: a service that could also reach notifications would be one
    that could act on a change it just made, and changing a preference must
    not touch a single existing row (§10 — it decides what is created next,
    never what already exists).
    """
    return NotificationPreferenceService(
        preferences=SqlAlchemyNotificationPreferenceRepository(session, availability=availability),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        availability=availability,
    )


NotificationPreferenceServiceDep = Annotated[
    NotificationPreferenceService, Depends(get_notification_preference_service)
]


def get_push_subscription_service(
    session: DbSessionDep,
    clock: ClockDep,
    availability: ChannelAvailabilityDep,
    settings: SettingsDep,
) -> PushSubscriptionService:
    """The push settings service — A64-021.6 §4, §20.

    Per request, over the request's session. Holds only the subscription
    repository: a service that could also reach deliveries would be one that
    could send a push from a settings request, and registering a device must
    not notify anybody.

    The **public** key is passed in and the private one is not — this
    service serves the key a browser subscribes with and has no reason to
    hold the one that signs. `platform.push` owns the signing key and is
    reached only by the delivery worker.
    """
    return PushSubscriptionService(
        subscriptions=SqlAlchemyPushSubscriptionRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        availability=availability,
        vapid_public_key=settings.push.vapid_public_key,
    )


PushSubscriptionServiceDep = Annotated[
    PushSubscriptionService, Depends(get_push_subscription_service)
]


def build_durable_notification_writer(
    session: AsyncSession,
    *,
    announcer: NotificationAnnouncer | None = None,
    availability: ChannelAvailability = IN_APP_ONLY,
) -> DurableNotificationWriter:
    """The sink that keeps notifications — A64-021.1 §12, A64-021.2 §1.

    Not a `Depends`, for the same reason the dispatcher below is not: it is
    built per relay tick over the worker's own session, and there is no
    request to scope it to. A `Depends`-shaped factory that nothing resolves
    through FastAPI would be a misleading signature.

    Built **per tick** rather than once per process, unlike the announcer:
    this one holds a repository, a repository holds a session, and a session
    must not outlive the unit of work it serves. The announcer holds a
    socket registry and a bus, so it is process-wide and passed in.

    `announcer` defaults to `NullNotificationAnnouncer` — *not* a testing
    convenience. A process with no fleet to announce into is a legitimate
    deployment (a relay worker without a gateway) and a contract suite is
    another, and the default makes "there is no realtime here" the explicit
    behaviour rather than a `None` check inside the writer.

    The delivery policy, by contrast, has **no** switch and no default
    (A64-021.3 §10). A build in which preferences could be bypassed by
    constructing the writer differently would be one where "I turned this
    off" depends on which process handled the event, so the only writer this
    platform can assemble is one that asks. It reads through the same
    session, so the preference consulted is the one committed.
    """
    return DurableNotificationWriter(
        notifications=SqlAlchemyNotificationRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        announcer=announcer if announcer is not None else NullNotificationAnnouncer(),
        policy=PreferenceDeliveryPolicy(
            preferences=SqlAlchemyNotificationPreferenceRepository(
                session, availability=availability
            )
        ),
        deliveries=SqlAlchemyEmailDeliveryRepository(session),
        push_deliveries=SqlAlchemyPushDeliveryRepository(session),
        subscriptions=SqlAlchemyPushSubscriptionRepository(session),
        availability=availability,
    )


def build_email_delivery_service(
    session: AsyncSession,
    *,
    provider: EmailProvider,
    metrics: MetricsRecorder,
    clock: Clock,
    availability: ChannelAvailability,
    settings: NotificationEmailSettings,
    public_url: str,
) -> EmailDeliveryService:
    """The email worker's service, over one pass's session — A64-021.5 §24.

    Every collaborator is this module's own except two: the transport, which
    is `platform`'s and is chosen at the composition root, and the recipient
    directory, which is `users`' published port and is passed in for the same
    reason the profile renderer is — this module must not name a `users`
    repository.

    Built per pass rather than per process, like every other worker service
    here: it holds repositories, repositories hold a session, and a session
    must not outlive the unit of work it serves.
    """
    return EmailDeliveryService(
        deliveries=SqlAlchemyEmailDeliveryRepository(session),
        notifications=SqlAlchemyNotificationRepository(session),
        recipients=SqlAlchemyEmailRecipientDirectory(session),
        policy=PreferenceDeliveryPolicy(
            preferences=SqlAlchemyNotificationPreferenceRepository(
                session, availability=availability
            )
        ),
        # The **canonical** frontend origin — `PUBLIC_APP_URL` on
        # `AppSettings`, not a second copy on this module's settings. Three
        # settings holding the same host is three chances for an email to
        # link a player into the wrong tier.
        renderer=TemplateEmailRenderer(public_origin=public_url),
        provider=provider,
        metrics=metrics,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        availability=availability,
        batch_size=settings.batch_size,
        max_attempts=settings.max_attempts,
        retry_base_seconds=settings.retry_base_seconds,
        retry_max_seconds=settings.retry_max_seconds,
    )


def build_push_delivery_service(
    session: AsyncSession,
    *,
    provider: PushProvider | None,
    metrics: MetricsRecorder,
    clock: Clock,
    availability: ChannelAvailability,
    settings: PushSettings,
) -> PushDeliveryService:
    """The push worker's service, over one pass's session — A64-021.6 §10.

    Every collaborator is this module's own except the transport, which is
    `platform`'s and is chosen at the composition root. Unlike the email
    worker there is **no recipient directory**: a push needs no address —
    the subscription is the address — and no renderer, because the payload
    is two identifiers rather than a rendered message.

    `provider` may be `None`, which is not a defect: a process with no VAPID
    key pair settles its claimed rows as `SKIPPED_CHANNEL_UNAVAILABLE`,
    which is true and is what an operator needs to see. Refusing to
    construct would take the worker down instead.

    Built per pass rather than per process, like every other worker service
    here: it holds repositories, repositories hold a session, and a session
    must not outlive the unit of work it serves.
    """
    return PushDeliveryService(
        deliveries=SqlAlchemyPushDeliveryRepository(session),
        subscriptions=SqlAlchemyPushSubscriptionRepository(session),
        policy=PreferenceDeliveryPolicy(
            preferences=SqlAlchemyNotificationPreferenceRepository(
                session, availability=availability
            )
        ),
        provider=provider,
        metrics=metrics,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        availability=availability,
        batch_size=settings.batch_size,
        max_attempts=settings.max_attempts,
        retry_base_seconds=settings.retry_base_seconds,
        retry_max_seconds=settings.retry_max_seconds,
        ttl_seconds=settings.ttl_seconds,
    )


def build_push_delivery_reader(session: AsyncSession) -> PushDeliveryRepository:
    """The operator's read-only handle.

    Typed as the **port**, so a diagnostic command can count deliveries by
    status and cannot claim one, send one, revoke a device, or learn whose
    it is.
    """
    return SqlAlchemyPushDeliveryRepository(session)


def build_email_delivery_reader(session: AsyncSession) -> EmailDeliveryRepository:
    """The operator's read-only handle — §21.

    Typed as the **port**, so a diagnostic command can count deliveries by
    status and cannot claim one, send one, or learn whose it is.
    """
    return SqlAlchemyEmailDeliveryRepository(session)


def build_tournament_notification_dispatcher(
    *,
    tournaments: TournamentNotificationReader,
    store: DurableNotificationStore,
) -> TournamentNotificationDispatcher:
    """The tournament consumer — A64-021.4 §11.

    Takes both collaborators rather than a session, unlike its social
    sibling: the reader is `tournament`'s to construct and this module must
    not name a `tournament` repository (`tournament-internals-are-private`).
    `app_factory` builds each side from its own module's root and hands them
    over, which is the same arrangement `build_social_notification_dispatcher`
    uses for the profile renderer.
    """
    return TournamentNotificationDispatcher(tournaments=tournaments, store=store)


def build_game_notification_dispatcher(
    *, profiles: BatchProfileRenderer, store: DurableNotificationStore
) -> GameNotificationDispatcher:
    """The game consumer — A64-021.4 §19.

    Holds a profile renderer and somewhere durable, and nothing that can
    reach a match: the completion event carries everything about the game,
    so a consumer able to read `game`'s tables would be one that could
    disagree with the event it was handed.
    """
    return GameNotificationDispatcher(profiles=profiles, store=store)


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
    "NotificationPreferenceServiceDep",
    "NotificationServiceDep",
    "PresenceNotificationServiceDep",
    "ChannelAvailabilityDep",
    "channel_availability_for",
    "email_channel_available",
    "build_durable_notification_writer",
    "build_email_delivery_reader",
    "build_email_delivery_service",
    "get_channel_availability",
    "build_game_notification_dispatcher",
    "build_social_notification_dispatcher",
    "build_tournament_notification_dispatcher",
    "get_notification_preference_service",
    "get_notification_service",
    "get_presence_notification_service",
]
