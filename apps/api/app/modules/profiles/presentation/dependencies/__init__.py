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
    NoMatchesStatisticsProvider
      -> ProfileService

**Two of the three collaborators are placeholders**, and this file is the
only place that will change when they stop being. `rating` ships, one line
here points at its adapter, and nothing in `application/` or `domain/`
moves. That is the payoff for the ports in `application/ports.py`.

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

from typing import Annotated

from fastapi import Depends

from app.api.deps import ClockDep, DbSessionDep
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.profiles.application.ports import RatingProvider, StatisticsProvider
from app.modules.profiles.application.services import ProfileService
from app.modules.profiles.infrastructure import (
    NoMatchesStatisticsProvider,
    UnratedRatingProvider,
)
from app.modules.users.application.services import UserService
from app.modules.users.application.services.preferences_service import PreferencesService
from app.modules.users.application.services.privacy_settings_service import (
    PrivacySettingsService,
)
from app.modules.users.application.services.profile_editing_service import ProfileEditingService
from app.modules.users.application.services.public_profile_service import PublicProfileService
from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository
from app.modules.users.public import (
    PreferencesEditor,
    PrivacySettingsEditor,
    ProfileEditor,
    PublicProfileReader,
)


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
    each marked provisional. See `infrastructure/unrated_providers.py`.

    Stateless, so a per-request instance costs one attribute assignment.
    """
    return UnratedRatingProvider()


RatingProviderDep = Annotated[RatingProvider, Depends(get_rating_provider)]


def get_statistics_provider() -> StatisticsProvider:
    """Match counts — **placeholder until the `statistics` module
    exists.** Returns zeroes, and therefore a `win_rate` of `0.0`."""
    return NoMatchesStatisticsProvider()


StatisticsProviderDep = Annotated[StatisticsProvider, Depends(get_statistics_provider)]


def get_profile_service(
    profiles: PublicProfileReaderDep,
    ratings: RatingProviderDep,
    statistics: StatisticsProviderDep,
) -> ProfileService:
    """The composed read use case.

    Every collaborator arrives already resolved rather than being built
    inline, so this factory cannot accidentally construct a second
    `UserService` on a different session — the mistake `auth`'s
    `get_password_reset_service` documents at length.
    """
    return ProfileService(profiles=profiles, ratings=ratings, statistics=statistics)


ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]


__all__ = [
    "PreferencesEditorDep",
    "PrivacySettingsEditorDep",
    "ProfileEditorDep",
    "ProfileServiceDep",
    "PublicProfileReaderDep",
    "RatingProviderDep",
    "StatisticsProviderDep",
    "get_preferences_editor",
    "get_privacy_settings_editor",
    "get_profile_editor",
    "get_profile_service",
    "get_public_profile_reader",
    "get_rating_provider",
    "get_statistics_provider",
]
