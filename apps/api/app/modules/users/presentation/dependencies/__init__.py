"""The FastAPI `Depends` bridge for this module — dependency-injection.md
DI-01: `Depends` is used *only* at the routing layer, to hand a route an
already-resolved service. It is not the container.

That distinction is why this file exists here rather than the router
constructing a `UserService` inline: the same service must be resolvable
by a future Celery task or admin tool that has no HTTP request and no
`Depends` at all. Those callers will construct the identical object graph
through `app.core.di.Container`; this module is only the HTTP-shaped half
of that bridge, and nothing in `application/` or `domain/` knows it exists.

The graph assembled here, per request:

    AsyncSession        opened by `app.api.deps.get_db_session` (one per
                        request — DI-02's rule that a session is scoped to
                        a command, never to a connection)
      -> SqlAlchemyUserRepository   the port's adapter
      -> SessionUnitOfWork          the transaction boundary over that
                                    same session, so the service can
                                    commit without touching SQLAlchemy
      -> UserService                the use cases
"""

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep, PresenceSettingsDep, RedisPoolsDep, get_clock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.users.application.ports import UserRepository
from app.modules.users.application.services import UserService
from app.modules.users.application.services.presence_service import PresenceService
from app.modules.users.application.services.public_profile_service import PublicProfileService
from app.modules.users.infrastructure.presence import (
    NoPresenceProvider,
    RedisPresenceProvider,
)
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from app.modules.users.public import PresenceProvider, PresenceRecorder, PublicProfileReader

# `get_clock` and `ClockDep` moved to `app.api.deps` in A64-011.9 — "now"
# is a platform concern, not this module's, and `auth` was importing them
# from here, which meant reaching into another module's private
# presentation package (R-1). Re-exported under the original names so this
# module's own routes and every test that overrides `get_clock` are
# unaffected by where it lives.
__all__ = [
    "ClockDep",
    "PublicProfileReaderDep",
    "UserRepositoryDep",
    "UserServiceDep",
    "get_clock",
    "get_public_profile_reader",
]


def get_user_repository(session: DbSessionDep) -> UserRepository:
    return SqlAlchemyUserRepository(session)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]


def get_user_service(
    session: DbSessionDep,
    users: UserRepositoryDep,
    clock: ClockDep,
) -> UserService:
    # The unit of work wraps the *same* session the repository holds —
    # otherwise the service would commit a transaction the repository
    # never wrote to, and the write would be silently lost on request
    # teardown.
    return UserService(
        users=users,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


def get_public_profile_reader(users: UserServiceDep) -> PublicProfileReader:
    """The **stranger's** view of a player, request-scoped — A64-020.6 §26.

    Published from this module's own root rather than assembled by each
    consumer, because `tournament` cannot assemble it: its
    `tournament-reaches-modules-through-public` contract forbids
    `app.modules.users.application`, which is where `PublicProfileService`
    lives. `game`, `matchmaking` and `profiles` each construct their own
    copy today and are left alone — this adds the door rather than
    rearranging the rooms behind it.

    Typed as the port, so a holder can read the public view and can do
    nothing else. There is no method here that reaches an email address,
    which is what makes A64-020.6 §27's "no self-only profile fields" true
    by construction rather than by review.
    """
    return PublicProfileService(users)


PublicProfileReaderDep = Annotated[PublicProfileReader, Depends(get_public_profile_reader)]


def get_presence_service(
    pools: RedisPoolsDep, settings: PresenceSettingsDep, clock: ClockDep
) -> PresenceService:
    """The presence **producer** — A64-013.6.

    The one factory on the platform that yields something holding
    `PresenceRecorder`. `profiles` gets the reader alone, precisely so the
    module serving anonymous traffic cannot assert that somebody is online;
    this is what `auth`'s lifecycle routes are handed.

    ## Why it lives in `users` and is wired from `auth`

    domain-model.md §299 assigns `Presence` to this module, so the service
    that writes it belongs here. The *events* that produce it — a sign-in, a
    refresh, a full sign-out — are `auth`'s, which is why `auth`'s routes
    resolve this dependency rather than `users` observing something it
    cannot see.

    That is the same shape `profiles` already uses for
    `avatars.presentation.dependencies.AvatarLinkBuilderDep`: a module's
    presentation layer resolving another module's published capability.

    ## The same two branches every presence dependency has

    `RedisPresenceProvider` is both the reader and the recorder, and it is
    handed the **`cache`** Redis role — see `PresenceSettings` for the AD-03
    argument. `NoPresenceProvider` is the fallback, wired by
    `PRESENCE_ENABLED=false`; it accepts writes and discards them silently,
    which is what a kill switch for a cosmetic feature must do rather than
    breaking a sign-in.

    Not logged at selection here, unlike `profiles`' presence factory: that
    one is the *only* place that knows a choice was made for a read path,
    and duplicating its `WARNING` on every login would double the noise for
    the same fact.
    """
    if not settings.enabled:
        recorder: PresenceRecorder = NoPresenceProvider()
        provider: PresenceProvider = NoPresenceProvider()
    else:
        redis_presence = RedisPresenceProvider(pools.cache, settings=settings, clock=clock)
        recorder = redis_presence
        provider = redis_presence

    return PresenceService(recorder=recorder, provider=provider)


PresenceServiceDep = Annotated[PresenceService, Depends(get_presence_service)]


def get_presence_recorder(
    pools: RedisPoolsDep, settings: PresenceSettingsDep, clock: ClockDep
) -> PresenceRecorder:
    """The presence **write port**, alone — A64-016.1.

    `PresenceService` bundles the recorder with the reader because `auth`'s
    lifecycle routes want both: a sign-in records presence and `GET
    /profile/me` reads the caller's own back. The gateway wants only the
    write half, and the difference is a capability rather than a
    convenience — a transport tier that could *read* presence could answer
    "is this player online" without going through the privacy composition
    that decides who may know, which is the exact split `profiles` already
    keeps by holding the reader alone.

    Same two branches and the same `cache` role as `get_presence_service`,
    because it is the same adapter behind a narrower type.
    """
    if not settings.enabled:
        return NoPresenceProvider()
    return RedisPresenceProvider(pools.cache, settings=settings, clock=clock)


PresenceRecorderDep = Annotated[PresenceRecorder, Depends(get_presence_recorder)]
