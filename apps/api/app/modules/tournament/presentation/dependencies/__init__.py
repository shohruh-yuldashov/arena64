"""The `Depends` bridge and background composition for `tournament`.

Two factories, and the split is the *caller* rather than the graph: one is
built per request over the request's session, the other per sweep over a
session the task opens. Both name their collaborators concretely here,
which is what a composition root is for — the services hold ports and can
reach nothing else.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ClockDep, DbSessionDep
from app.api.outbox_deps import EventPublisherDep
from app.core.clock import Clock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.rating.presentation.dependencies import get_rating_reader
from app.modules.tournament.application.services.registration_service import (
    TournamentDeadlineService,
    TournamentRegistrationService,
)
from app.modules.tournament.application.services.seeding_service import (
    TournamentSeedingService,
)
from app.modules.tournament.infrastructure.rating_snapshots import (
    PublishedRatingSnapshots,
)
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyPairingRepository,
    SqlAlchemyRegistrationRepository,
    SqlAlchemySeedRepository,
    SqlAlchemyTournamentRepository,
)
from app.modules.users.presentation.dependencies import UserServiceDep
from app.modules.users.public import UserProfileService
from app.platform.outbox import EventPublisher


def get_registration_service(
    session: DbSessionDep,
    players: UserServiceDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> TournamentRegistrationService:
    """The registration use cases, over this request's session.

    The outbox publisher is built over the **same** session as the
    repositories, which is what puts a lifecycle event in the transaction
    that caused it rather than beside it (AD-16).
    """
    return TournamentRegistrationService(
        tournaments=SqlAlchemyTournamentRepository(session),
        registrations=SqlAlchemyRegistrationRepository(session),
        players=UserProfileService(players),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_deadline_service(
    session: AsyncSession, *, events: EventPublisher, clock: Clock
) -> TournamentDeadlineService:
    """The deadline sweep, for the scheduled task — §9.

    Takes plain arguments rather than resolving `Depends`, for the reason
    `build_queue_service` and `build_gateway_service` do: the background
    path has no request to resolve against, and a factory reachable only
    through `Depends` is one a worker assembles its own copy of — which
    drifts on the first collaborator either gains.
    """
    return TournamentDeadlineService(
        tournaments=SqlAlchemyTournamentRepository(session),
        registrations=SqlAlchemyRegistrationRepository(session),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def get_seeding_service(
    session: DbSessionDep, events: EventPublisherDep, clock: ClockDep
) -> TournamentSeedingService:
    """Seeding and first-round planning — A64-019.3 §11.

    `PublishedRatingSnapshots` is named here, which is what a composition
    root is for: the service holds `RatingSnapshots`, a one-method port, so
    it can read a batch of ratings and cannot reach an adjustment, a
    leaderboard, or anything that writes.

    **No production entry point yet.** Nothing calls this in the running
    application: A64-019.4 drives it when a tournament starts. It is
    composed now so that phase wires a factory rather than inventing one,
    and the reachability registry is deliberately unchanged — see the
    phase report.
    """
    return TournamentSeedingService(
        tournaments=SqlAlchemyTournamentRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        pairings=SqlAlchemyPairingRepository(session),
        ratings=PublishedRatingSnapshots(get_rating_reader(session)),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


TournamentSeedingServiceDep = Annotated[TournamentSeedingService, Depends(get_seeding_service)]

TournamentRegistrationServiceDep = Annotated[
    TournamentRegistrationService, Depends(get_registration_service)
]


__all__ = [
    "TournamentRegistrationServiceDep",
    "TournamentSeedingServiceDep",
    "get_seeding_service",
    "build_deadline_service",
    "get_registration_service",
]
