"""The FastAPI `Depends` bridge for `profiles` — dependency-injection.md
DI-01: `Depends` is used only at the routing layer, to hand a route an
already-resolved service. It is not the container.

The graph assembled per request:

    AsyncSession              one per request (`app.api.deps`)
      -> SqlAlchemyUserRepository
      -> SessionUnitOfWork    unused on this path — see below
      -> UserService
      -> PublicProfileService adapts it to the published port
    UnratedRatingProvider     placeholder, stateless
    DatabaseStatisticsProvider
    RedisPresenceProvider     the `cache` Redis role, A64-012.7
      -> ProfileService

**One of the four collaborators is still a placeholder**, and this file is
the only place that will change when it stops being. `rating` ships, one
line here points at its adapter, and nothing in `application/` or `domain/`
moves. That is the payoff for the ports in `application/ports.py`.

Two of them have a kill switch and therefore two branches each — see
`get_statistics_provider` and `get_presence_provider`, which are the only
places on the platform that know a fallback was chosen.

## Why a unit of work is constructed for a read-only path

`UserService` requires one, because most of its use cases write. This one
does not: `find_by_username` opens no transaction, so the object is
constructed and never entered.

Passing it anyway rather than reaching for a null implementation keeps
`profiles` from having an opinion about `users`' internals — the day
`find_by_username` needs to touch a transaction (a read-through cache
write, a lookup counter), that is `users`' business and this file does not
need to know it happened.

## Why `profiles` builds `users`' internals here

The same reason `auth` does, and it looks like a boundary violation for the
same reason it is not: this is the *composition root's* job, and a
composition root is the one place permitted to know how to construct
things (BR-6 forbids a *module* reaching for the container, not the root
wiring modules together). `ProfileService` itself sees only
`users.public.PublicProfileReader` and never learns that a `UserService`
exists.
"""

import logging
from typing import Annotated

from fastapi import Depends

from app.api.deps import (
    ClockDep,
    DbSessionDep,
    FriendsSettingsDep,
    PresenceSettingsDep,
    RedisPoolsDep,
    StatisticsSettingsDep,
)
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.application.services import SocialGraphReaderService
from app.modules.friends.infrastructure.repositories import (
    SqlAlchemyBlockedPlayerRepository,
    SqlAlchemyFriendshipRepository,
)
from app.modules.profiles.application.ports import (
    BlockedPlayersProvider,
    RatingProvider,
    StatisticsProvider,
    ViewerRelationshipProvider,
)
from app.modules.profiles.application.services import ProfileService
from app.modules.profiles.application.services.profile_composer import PublicProfileComposer
from app.modules.profiles.application.services.profile_directory_service import (
    ProfileDirectoryService,
)
from app.modules.profiles.application.services.profile_search_service import ProfileSearchService
from app.modules.profiles.infrastructure import (
    DatabaseStatisticsProvider,
    FriendshipRelationshipProvider,
    NoBlockedPlayersProvider,
    NoMatchesStatisticsProvider,
    NoRelationshipsProvider,
    SocialGraphBlockedPlayersProvider,
    UnratedRatingProvider,
)
from app.modules.statistics.application.services import StatisticsService
from app.modules.statistics.infrastructure.repositories import SqlAlchemyStatisticsRepository
from app.modules.users.application.services import UserService
from app.modules.users.application.services.preferences_service import PreferencesService
from app.modules.users.application.services.privacy_settings_service import (
    PrivacySettingsService,
)
from app.modules.users.application.services.profile_editing_service import ProfileEditingService
from app.modules.users.application.services.public_profile_search_service import (
    PublicProfileSearchService,
)
from app.modules.users.application.services.public_profile_service import PublicProfileService
from app.modules.users.infrastructure.presence import (
    NoPresenceProvider,
    RedisPresenceProvider,
)
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from app.modules.users.public import (
    PreferencesEditor,
    PresenceProvider,
    PrivacySettingsEditor,
    ProfileEditor,
    PublicProfileReader,
    PublicProfileSearcher,
)

logger = logging.getLogger(__name__)


def get_public_profile_reader(session: DbSessionDep, clock: ClockDep) -> PublicProfileReader:
    """`users`' side of the profile lookup, behind its sixth published
    port.

    Assembled separately from the five `auth` builds for the reason they
    are separate from each other: reading the *public* view is a distinct
    capability from reading an account's own view, and the type that
    crosses this boundary has no `email` field. A single factory returning
    something that satisfied both ports would hand an anonymous-traffic
    module the ability to read addresses.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return PublicProfileService(users)


PublicProfileReaderDep = Annotated[PublicProfileReader, Depends(get_public_profile_reader)]


def get_profile_editor(session: DbSessionDep, clock: ClockDep) -> ProfileEditor:
    """`users`' side of self-service profile editing, behind its eighth
    published port.

    Assembled separately from `get_public_profile_reader` above even though
    both read a profile, for the reason every port pair on this platform is
    separate: that one serves anonymous callers and returns a shape with no
    timezone and no email; this one serves the owner, returns `OwnUserProfile`,
    and can *write*. A single factory returning something that satisfied
    both would hand the anonymous-traffic path a write capability.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return ProfileEditingService(users)


ProfileEditorDep = Annotated[ProfileEditor, Depends(get_profile_editor)]


def get_privacy_settings_editor(session: DbSessionDep, clock: ClockDep) -> PrivacySettingsEditor:
    """`users`' side of the privacy controls, behind its ninth published
    port.

    A third factory over the same three objects, and the third one is not
    redundant for the reason the second was not: what differs is the
    *capability* the caller receives, not the graph underneath it. This one
    can read and write five booleans and can do nothing else — it cannot
    read a biography, cannot rename anybody, and cannot see another
    account's settings.

    Collapsing the three into one factory returning a `UserService` would
    save twenty lines and would hand every route on this module the union
    of every capability, which is the whole thing the published ports
    exist to prevent.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return PrivacySettingsService(users)


PrivacySettingsEditorDep = Annotated[PrivacySettingsEditor, Depends(get_privacy_settings_editor)]


def get_preferences_editor(session: DbSessionDep, clock: ClockDep) -> PreferencesEditor:
    """`users`' side of the personal settings, behind its tenth published
    port.

    A fourth factory over the same three objects, and the justification is
    the one the second and third gave: what differs is the *capability*,
    not the graph. This one can read and write two preference groups and
    nothing else — it cannot rename anybody, cannot read a biography, and
    cannot change what strangers see.

    It is also the only factory on the platform that yields something able
    to change a language or a timezone, which is what A64-012.5's "avoid
    duplicated writable fields" looks like once it reaches the composition
    root.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return PreferencesService(users)


PreferencesEditorDep = Annotated[PreferencesEditor, Depends(get_preferences_editor)]


def get_rating_provider() -> RatingProvider:
    """Ratings — **placeholder until the `rating` module exists.**

    Returns every player as unrated: the starting value in each category,
    each marked provisional. See `infrastructure/rating_providers.py`.

    Stateless, so a per-request instance costs one attribute assignment.
    """
    return UnratedRatingProvider()


RatingProviderDep = Annotated[RatingProvider, Depends(get_rating_provider)]


def get_statistics_provider(
    session: DbSessionDep, settings: StatisticsSettingsDep
) -> StatisticsProvider:
    """A player's competitive record — **real since A64-012.6.**

    This is the selection point A64-012.6 asks to be logged, and it is the
    only place that knows a *choice* was made: neither provider can say
    what it was chosen instead of.

    ## The two branches

    `DatabaseStatisticsProvider` is the default. It is handed a
    `StatisticsReader` — `statistics`' published port, assembled here from
    that module's own service and repository, because a composition root is
    the one place permitted to know how to construct things (BR-6 forbids a
    *module* reaching for the container, not the root wiring modules
    together). `ProfileService` sees only
    `profiles.application.ports.StatisticsProvider` and never learns that a
    `statistics` schema exists.

    `NoMatchesStatisticsProvider` is the fallback, wired when
    `STATISTICS_ENABLED=false`. It exists for a store being rebuilt or a
    store that is unhealthy — `player_statistics` is a projection and
    rebuildable by definition (database.md C5), so the sane failure mode is
    a profile page without numbers rather than no profile page at all,
    which is the platform's highest-volume public read (§1436).

    ## Why the fallback logs at WARNING and the normal path does not

    Serving blank statistics to everybody is a degradation nobody can see
    from a response — a player with a real record looks exactly like a new
    account. `WARNING` on every request makes "we are currently serving
    blank statistics" an alertable condition; an `INFO` line on the healthy
    path would fire on every profile read and be no signal at all
    (services.md §7.1).
    """
    if not settings.enabled:
        logger.warning("statistics_provider_fallback", extra={"provider": "no_matches"})
        return NoMatchesStatisticsProvider()

    logger.debug("statistics_provider_selected", extra={"provider": "database"})
    reader = StatisticsService(SqlAlchemyStatisticsRepository(session))
    return DatabaseStatisticsProvider(reader)


StatisticsProviderDep = Annotated[StatisticsProvider, Depends(get_statistics_provider)]


def get_presence_provider(
    pools: RedisPoolsDep,
    settings: PresenceSettingsDep,
    clock: ClockDep,
) -> PresenceProvider:
    """Whether a player is here right now — A64-012.7.

    The second selection point on this module, and it is logged for the
    reason `get_statistics_provider` above is: this is the only place that
    knows a *choice* was made, because neither provider can say what it was
    chosen instead of.

    ## The two branches

    `RedisPresenceProvider` is the default. It is handed the **`cache`**
    Redis role — never `live`, and never `limits`. Presence is derived,
    expendable and self-expiring, and losing it is a cosmetic defect
    (system-design.md §626), which is exactly the posture `cache` is
    configured for. A reconnect storm writing one key per returning player
    must not compete with the positions of games in progress on `live`
    (AD-03's own worked example), and `limits` is deliberately configured to
    evict nothing, which is the opposite of what this workload wants. See
    `PresenceSettings` for the argument in full and for when a dedicated
    sixth role becomes warranted.

    `NoPresenceProvider` is the fallback, wired when `PRESENCE_ENABLED=false`
    — for a presence instance being replaced or resized. Every profile then
    reports `is_online: null` and `last_seen: null`.

    ## Why the fallback logs at WARNING and the normal path does not

    An operator should be able to see that presence is switched off, because
    nothing in a response says so. `WARNING` makes it an alertable
    condition; an `INFO` line on the healthy path would fire on every profile
    read and be no signal at all (services.md §7.1).

    It is a **quieter** warning than the statistics one, and the difference
    is worth stating rather than inferring. Blank statistics are a lie — a
    player with a real record looks like a beginner. Unknown presence is the
    same `null` a profile already reports for a player who is offline or who
    has hidden it, so this degradation misinforms nobody; it merely removes
    a feature.

    Returns the **provider**, not the recorder. Nothing on the HTTP surface
    is handed `PresenceRecorder` — the two ports are separate so that the
    module serving anonymous traffic cannot mark accounts online, and this
    return type is where that separation is enforced rather than intended.
    """
    if not settings.enabled:
        logger.warning("presence_provider_fallback", extra={"provider": "none"})
        return NoPresenceProvider()

    logger.debug("presence_provider_selected", extra={"provider": "redis"})
    return RedisPresenceProvider(pools.cache, settings=settings, clock=clock)


PresenceProviderDep = Annotated[PresenceProvider, Depends(get_presence_provider)]


def _social_graph(session: DbSessionDep) -> SocialGraphReaderService:
    """`friends`' published reader, assembled over this request's session.

    A helper because two providers need the same object, and building it
    twice would mean two identity maps over the same rows in one request.

    Not a `Depends` factory of its own: it returns `friends`' type, and a
    dependency yielding it would publish that module's service into this
    module's dependency namespace for no caller's benefit. What the two
    providers below expose are `profiles`' own ports.
    """
    return SocialGraphReaderService(
        friendships=SqlAlchemyFriendshipRepository(session),
        blocks=SqlAlchemyBlockedPlayerRepository(session),
    )


def get_relationship_provider(
    session: DbSessionDep, settings: FriendsSettingsDep
) -> ViewerRelationshipProvider:
    """What a viewer is to the players they are reading — A64-013.3.

    The third selection point on this module, and it is logged for the
    reason the other two are: this is the only place that knows a *choice*
    was made, because neither provider can say what it was chosen instead
    of.

    ## The two branches

    `FriendshipRelationshipProvider` is the default. It is handed
    `friends.public.FriendshipReader` — that module's published port,
    assembled here from its own repository, because a composition root is
    the one place permitted to know how to construct things (BR-6 forbids a
    *module* reaching for the container, not the root wiring modules
    together). `PublicProfileComposer` sees only
    `profiles.application.ports.ViewerRelationshipProvider` and never learns
    that a `friends` schema exists.

    `NoRelationshipsProvider` is the fallback, wired when
    `FRIENDS_ENABLED=false` — for a social graph being migrated or one that
    is unhealthy.

    ## The degradation narrows, which is why it is safe

    With every viewer a stranger, a field set to `FRIENDS` is hidden from
    everyone including actual friends. That is a visible loss of
    functionality and not a disclosure — the correct direction for a privacy
    control to fail in, and the opposite of what "everybody is a friend"
    would do during an incident.

    `WARNING` at selection, because nothing in a response says the graph is
    off: a friends-only field simply looks hidden.
    """
    if not settings.enabled:
        logger.warning("relationship_provider_fallback", extra={"provider": "none"})
        return NoRelationshipsProvider()

    logger.debug("relationship_provider_selected", extra={"provider": "friendship"})
    return FriendshipRelationshipProvider(_social_graph(session))


RelationshipProviderDep = Annotated[ViewerRelationshipProvider, Depends(get_relationship_provider)]


def get_blocked_players_provider(
    session: DbSessionDep, settings: FriendsSettingsDep
) -> BlockedPlayersProvider:
    """Who a viewer must never be shown — A64-013.5.

    Chosen by the same switch as the relationship provider, because they
    read the same graph and a deployment cannot sensibly have one without
    the other.

    `NoBlockedPlayersProvider` is the fallback and it **never fabricates a
    block**: with the graph off, nobody is excluded from search. That is the
    lesser harm and the only honest option — a fallback that invented
    restrictions from missing data would hide the platform from itself
    during an incident. The *visibility* consequence still holds anyway,
    because `NoRelationshipsProvider` reports `STRANGER` rather than
    `FRIEND`, so every friends-only field stays hidden.

    `WARNING` on the fallback and `DEBUG` on the healthy path, for the
    reason every selection point on this module gives.
    """
    if not settings.enabled:
        logger.warning("blocked_players_provider_fallback", extra={"provider": "none"})
        return NoBlockedPlayersProvider()

    logger.debug("blocked_players_provider_selected", extra={"provider": "social_graph"})
    return SocialGraphBlockedPlayersProvider(_social_graph(session))


BlockedPlayersProviderDep = Annotated[BlockedPlayersProvider, Depends(get_blocked_players_provider)]


def get_profile_composer(
    ratings: RatingProviderDep,
    statistics: StatisticsProviderDep,
    presence: PresenceProviderDep,
    relationships: RelationshipProviderDep,
) -> PublicProfileComposer:
    """The public view, assembled from three sources with privacy applied —
    A64-013.1.

    Built once per request and shared by both read paths, which is the
    point: `GET /profiles/{username}` and `GET /users/search` are handed the
    *same object*, so there is no arrangement of dependencies in which one
    of them could be composed differently from the other.
    """
    return PublicProfileComposer(
        ratings=ratings,
        statistics=statistics,
        presence=presence,
        relationships=relationships,
    )


ProfileComposerDep = Annotated[PublicProfileComposer, Depends(get_profile_composer)]


def get_public_profile_searcher(session: DbSessionDep, clock: ClockDep) -> PublicProfileSearcher:
    """`users`' side of the player search, behind its thirteenth published
    port — A64-013.1.

    Assembled separately from `get_public_profile_reader` above even though
    both read public profiles and both return the same DTO, for the reason
    every port pair on this platform is separate: what differs is the
    *capability*. That one resolves a handle a caller already has; this one
    enumerates handles a caller does not. They are the same data and very
    different authority, which is why only this one sits behind
    authentication and a rate limit.
    """
    users = UserService(
        users=SqlAlchemyUserRepository(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )
    return PublicProfileSearchService(users)


PublicProfileSearcherDep = Annotated[PublicProfileSearcher, Depends(get_public_profile_searcher)]


def get_profile_service(
    profiles: PublicProfileReaderDep,
    composer: ProfileComposerDep,
    statistics: StatisticsProviderDep,
    presence: PresenceProviderDep,
) -> ProfileService:
    """The composed read use case.

    Every collaborator arrives already resolved rather than being built
    inline, so this factory cannot accidentally construct a second
    `UserService` on a different session — the mistake `auth`'s
    `get_password_reset_service` documents at length.

    The two providers arrive *beside* the composer rather than only inside
    it because `ProfileService` serves two owner-only reads that bypass
    privacy entirely — see its constructor on why those must not go through
    a gate that exists to apply it.
    """
    return ProfileService(
        profiles=profiles,
        composer=composer,
        statistics=statistics,
        presence=presence,
    )


def get_profile_directory(
    profiles: PublicProfileReaderDep,
    composer: ProfileComposerDep,
) -> ProfileDirectoryService:
    """Player ids to composed public profiles — A64-013.2.

    Exported for **other modules** to depend on, which is what separates it
    from the four factories above. `friends` renders a page of players it
    knows only by id, and this is how it does so without rebuilding
    composition: one dependency, four round trips per page, and the same
    privacy gate `GET /profiles/{username}` applies.

    Reached through `presentation/dependencies` rather than a `public/`
    port, matching how `avatars.presentation.dependencies.AvatarLinkBuilderDep`
    is already consumed by three modules. A composition root is the one
    place permitted to know how to construct things, and a `Depends` factory
    is that root's vocabulary.
    """
    return ProfileDirectoryService(profiles=profiles, composer=composer)


ProfileDirectoryDep = Annotated[ProfileDirectoryService, Depends(get_profile_directory)]


def get_profile_search_service(
    searcher: PublicProfileSearcherDep,
    composer: ProfileComposerDep,
    blocked_players: BlockedPlayersProviderDep,
) -> ProfileSearchService:
    """The search use case — A64-013.1.

    Two collaborators and no providers of its own. Everything about
    *rendering* a player is the composer's, and everything about *finding*
    one is the searcher's; this service holds the exclusion set and the
    logging contract and nothing else.
    """
    return ProfileSearchService(
        searcher=searcher, composer=composer, blocked_players=blocked_players
    )


ProfileSearchServiceDep = Annotated[ProfileSearchService, Depends(get_profile_search_service)]


ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]


__all__ = [
    "PreferencesEditorDep",
    "PresenceProviderDep",
    "PrivacySettingsEditorDep",
    "ProfileEditorDep",
    "ProfileComposerDep",
    "BlockedPlayersProviderDep",
    "RelationshipProviderDep",
    "ProfileDirectoryDep",
    "ProfileSearchServiceDep",
    "ProfileServiceDep",
    "PublicProfileSearcherDep",
    "PublicProfileReaderDep",
    "RatingProviderDep",
    "StatisticsProviderDep",
    "get_preferences_editor",
    "get_presence_provider",
    "get_privacy_settings_editor",
    "get_profile_editor",
    "get_profile_composer",
    "get_blocked_players_provider",
    "get_relationship_provider",
    "get_profile_directory",
    "get_profile_search_service",
    "get_profile_service",
    "get_public_profile_searcher",
    "get_public_profile_reader",
    "get_rating_provider",
    "get_statistics_provider",
]
