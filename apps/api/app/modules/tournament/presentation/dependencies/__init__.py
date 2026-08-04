"""The `Depends` bridge and background composition for `tournament`.

Two shapes of factory, and the split is the *caller* rather than the graph:
`get_*` is built per request over the request's session, `build_*` per sweep
or per relay tick over a session the caller opens. Both name this module's
own collaborators concretely, which is what a composition root is for — the
services hold ports and can reach nothing else.

## What this root deliberately cannot assemble

`game`'s concrete classes. `tournament-reaches-modules-through-public`
covers this package **including** its composition root, which is the one
place every other module's contract exempts — see `.importlinter`. So a
`MatchCreationUseCase` and an `OriginMatchReader` arrive as arguments from
`app_factory`, and nothing here can name a `game` table even by accident.

That is stricter than `matchmaking`'s equivalent and deliberately so: this
module is the newest consumer of `game.public`, and `services.md` §11.3's
claim that tournaments need no new mechanism holds only while the edge is
exactly `tournament -> game.public`.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ClockDep, DbSessionDep
from app.api.outbox_deps import EventPublisherDep
from app.core.clock import Clock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.public import MatchCreationUseCase
from app.modules.game.public.reconciliation import OriginMatchReader
from app.modules.rating.presentation.dependencies import get_rating_reader
from app.modules.tournament.application.services.advancement_service import (
    TournamentAdvancementService,
)
from app.modules.tournament.application.services.bracket_service import (
    TournamentBracketService,
)
from app.modules.tournament.application.services.match_completion_consumer import (
    TournamentMatchCompletionConsumer,
)
from app.modules.tournament.application.services.match_launcher import (
    TournamentMatchLauncher,
)
from app.modules.tournament.application.services.reconciliation_service import (
    TournamentReconciliationService,
)
from app.modules.tournament.application.services.registration_service import (
    TournamentDeadlineService,
    TournamentRegistrationService,
)
from app.modules.tournament.application.services.seeding_service import (
    TournamentSeedingService,
)
from app.modules.tournament.application.services.start_service import (
    TournamentStartService,
)
from app.modules.tournament.infrastructure.rating_snapshots import (
    PublishedRatingSnapshots,
)
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyBracketRepository,
    SqlAlchemyPairingAttemptRepository,
    SqlAlchemyPairingRepository,
    SqlAlchemyRegistrationRepository,
    SqlAlchemyRoundRepository,
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


def build_bracket_service(
    session: AsyncSession, *, events: EventPublisher, clock: Clock
) -> TournamentBracketService:
    """Bracket materialisation and winner advancement — A64-019.4.

    Plain arguments rather than `Depends`, for `build_deadline_service`'s
    reason: A64-019.5 drives this from a start and from an outbox consumer,
    neither of which has a request to resolve against.
    """
    return TournamentBracketService(
        tournaments=SqlAlchemyTournamentRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        pairings=SqlAlchemyPairingRepository(session),
        bracket=SqlAlchemyBracketRepository(session),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def get_bracket_service(
    session: DbSessionDep, events: EventPublisherDep, clock: ClockDep
) -> TournamentBracketService:
    """The same graph, over this request's session."""
    return build_bracket_service(session, events=events, clock=clock)


def build_match_launcher(
    session: AsyncSession, *, matches: MatchCreationUseCase, clock: Clock
) -> TournamentMatchLauncher:
    """The one edge from `tournament` into `game` — A64-019.5.

    `matches` is **passed in** rather than assembled here, and that is the
    import contract rather than a preference: `tournament-reaches-modules-
    through-public` covers this package's composition root too, so the only
    place permitted to name `game`'s concrete `MatchCreationUseCase` is
    `app_factory` — see that file's `_tournament_consumer_for`.

    Every other collaborator is this module's own, over the caller's
    session, so the attempt rows land in the caller's transaction.
    """
    return TournamentMatchLauncher(
        matches=matches,
        ratings=PublishedRatingSnapshots(get_rating_reader(session)),
        attempts=SqlAlchemyPairingAttemptRepository(session),
        clock=clock,
    )


def build_start_service(
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    events: EventPublisher,
    clock: Clock,
) -> TournamentStartService:
    """Starting a tournament — A64-019.5 §8.

    Composes `TournamentBracketService` rather than a second bracket-writing
    path, so materialisation has one implementation and one set of
    guarantees.
    """
    return TournamentStartService(
        tournaments=SqlAlchemyTournamentRepository(session),
        brackets=build_bracket_service(session, events=events, clock=clock),
        bracket=SqlAlchemyBracketRepository(session),
        rounds=SqlAlchemyRoundRepository(session),
        attempts=SqlAlchemyPairingAttemptRepository(session),
        launcher=build_match_launcher(session, matches=matches, clock=clock),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_advancement_service(
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    events: EventPublisher,
    clock: Clock,
) -> TournamentAdvancementService:
    """What a completed match does to a bracket — A64-019.5 §9, §10.

    Shared by the outbox consumer and the reconciler, deliberately: a repair
    and a delivery must not be able to disagree about what a result means.
    """
    return TournamentAdvancementService(
        tournaments=SqlAlchemyTournamentRepository(session),
        bracket=SqlAlchemyBracketRepository(session),
        brackets=build_bracket_service(session, events=events, clock=clock),
        rounds=SqlAlchemyRoundRepository(session),
        attempts=SqlAlchemyPairingAttemptRepository(session),
        launcher=build_match_launcher(session, matches=matches, clock=clock),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_match_completion_consumer(
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    events: EventPublisher,
    clock: Clock,
) -> TournamentMatchCompletionConsumer:
    """`game.match_completed` -> a bracket advancement, over one relay tick's
    session — A64-019.5 §9."""
    return TournamentMatchCompletionConsumer(
        build_advancement_service(session, matches=matches, events=events, clock=clock)
    )


def build_reconciliation_service(
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    origin_matches: OriginMatchReader,
    events: EventPublisher,
    clock: Clock,
) -> TournamentReconciliationService:
    """The recovery for the window BE-05 leaves open — A64-019.5 §10.

    Both `game` collaborators are published — a command and a read — and
    both arrive from the composition root for the same contract reason
    `build_match_launcher` records.
    """
    return TournamentReconciliationService(
        tournaments=SqlAlchemyTournamentRepository(session),
        bracket=SqlAlchemyBracketRepository(session),
        attempts=SqlAlchemyPairingAttemptRepository(session),
        origin_matches=origin_matches,
        launcher=build_match_launcher(session, matches=matches, clock=clock),
        advancement=build_advancement_service(session, matches=matches, events=events, clock=clock),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


TournamentBracketServiceDep = Annotated[TournamentBracketService, Depends(get_bracket_service)]

TournamentSeedingServiceDep = Annotated[TournamentSeedingService, Depends(get_seeding_service)]

TournamentRegistrationServiceDep = Annotated[
    TournamentRegistrationService, Depends(get_registration_service)
]


__all__ = [
    "TournamentRegistrationServiceDep",
    "TournamentBracketServiceDep",
    "TournamentSeedingServiceDep",
    "build_advancement_service",
    "build_bracket_service",
    "build_deadline_service",
    "build_match_completion_consumer",
    "build_match_launcher",
    "build_reconciliation_service",
    "build_start_service",
    "get_bracket_service",
    "get_registration_service",
    "get_seeding_service",
]
