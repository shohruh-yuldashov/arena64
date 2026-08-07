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

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import ClockDep, DbSessionDep
from app.api.outbox_deps import EventPublisherDep
from app.config.settings import TournamentSettings
from app.core.clock import Clock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.public import MatchCreationUseCase
from app.modules.game.public.reconciliation import OriginMatchReader
from app.modules.rating.presentation.dependencies import get_rating_reader
from app.modules.tournament.application.ports import (
    PlayerDirectory,
    TournamentDirectory,
    TournamentRepository,
)
from app.modules.tournament.application.services.advancement_service import (
    TournamentAdvancementService,
)
from app.modules.tournament.application.services.bracket_service import (
    TournamentBracketService,
)
from app.modules.tournament.application.services.completion_service import (
    TournamentCompletionService,
)
from app.modules.tournament.application.services.match_completion_consumer import (
    TournamentMatchCompletionConsumer,
)
from app.modules.tournament.application.services.match_launcher import (
    TournamentMatchLauncher,
)
from app.modules.tournament.application.services.no_show_service import (
    TournamentNoShowService,
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
from app.modules.tournament.infrastructure.repositories.notification_reader import (
    SqlAlchemyTournamentNotificationReader,
)
from app.modules.tournament.infrastructure.repositories.results_repository import (
    SqlAlchemyTournamentResults,
)
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyBracketRepository,
    SqlAlchemyPairingAttemptRepository,
    SqlAlchemyPairingRepository,
    SqlAlchemyRegistrationRepository,
    SqlAlchemyRoundRepository,
    SqlAlchemySeedRepository,
    SqlAlchemyStandingRepository,
    SqlAlchemyTournamentRepository,
)
from app.modules.tournament.public import TournamentAttendance, TournamentNotificationReader
from app.modules.users.presentation.dependencies import UserServiceDep
from app.modules.users.public import UserProfileService
from app.platform.outbox import EventPublisher


def build_registration_service(
    session: AsyncSession,
    *,
    players: PlayerDirectory,
    events: EventPublisher,
    clock: Clock,
) -> TournamentRegistrationService:
    """The registration use cases, over a session the caller opened.

    Plain arguments rather than `Depends`, for `build_deadline_service`'s
    reason — A64-019.8 gave this two callers with no request to resolve
    against: the participant routes (through `get_registration_service`
    below) and `app.operator.tournament`.

    The outbox publisher is built over the **same** session as the
    repositories, which is what puts a lifecycle event in the transaction
    that caused it rather than beside it (AD-16).
    """
    return TournamentRegistrationService(
        tournaments=SqlAlchemyTournamentRepository(session),
        registrations=SqlAlchemyRegistrationRepository(session),
        players=players,
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_tournament_reader(session: AsyncSession) -> TournamentRepository:
    """The aggregate, read-only from the caller's point of view.

    Typed as the **port**, so the operator profile can ask what state a
    tournament is in — which is how its lifecycle commands stay idempotent
    — without holding anything that could seed or start one.
    """
    return SqlAlchemyTournamentRepository(session)


def get_registration_service(
    session: DbSessionDep,
    players: UserServiceDep,
    events: EventPublisherDep,
    clock: ClockDep,
) -> TournamentRegistrationService:
    """The same graph, over this request's session — A64-019.8 §1.

    Reached by the two participant endpoints. `UserProfileService` is
    resolved here because `users` publishes it per request; the operator
    profile builds its own from the same class.
    """
    return build_registration_service(
        session, players=UserProfileService(players), events=events, clock=clock
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


def build_seeding_service(
    session: AsyncSession, *, events: EventPublisher, clock: Clock
) -> TournamentSeedingService:
    """Seeding and first-round planning — A64-019.3 §11.

    `PublishedRatingSnapshots` is named here, which is what a composition
    root is for: the service holds `RatingSnapshots`, a one-method port, so
    it can read a batch of ratings and cannot reach an adjustment, a
    leaderboard, or anything that writes.

    Reached by `app.operator.tournament seed` — A64-019.8 closed the gap
    where this was composed and called by nothing.
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


def get_seeding_service(
    session: DbSessionDep, events: EventPublisherDep, clock: ClockDep
) -> TournamentSeedingService:
    """The same graph, over this request's session."""
    return build_seeding_service(session, events=events, clock=clock)


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
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    clock: Clock,
    settings: TournamentSettings,
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
        no_show_seconds=settings.no_show_seconds,
    )


def build_start_service(
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    settings: TournamentSettings,
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
        launcher=build_match_launcher(session, matches=matches, clock=clock, settings=settings),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_completion_service(
    session: AsyncSession, *, events: EventPublisher, clock: Clock
) -> TournamentCompletionService:
    """Materialising a completed tournament's result — A64-019.6 §6f.

    Its own factory rather than a method on the advancement graph, because
    two very different callers reach it: the advancement flow, when a final
    gains a winner, and a read path that must be able to complete a bracket
    an operator is looking at. Both get the same object graph over their own
    session.
    """
    return TournamentCompletionService(
        tournaments=SqlAlchemyTournamentRepository(session),
        bracket=SqlAlchemyBracketRepository(session),
        attempts=SqlAlchemyPairingAttemptRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        standings=SqlAlchemyStandingRepository(session),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_advancement_service(
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    settings: TournamentSettings,
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
        launcher=build_match_launcher(session, matches=matches, clock=clock, settings=settings),
        completion=build_completion_service(session, events=events, clock=clock),
        events=events,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


def build_match_completion_consumer(
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    settings: TournamentSettings,
    events: EventPublisher,
    clock: Clock,
) -> TournamentMatchCompletionConsumer:
    """`game.match_completed` -> a bracket advancement, over one relay tick's
    session — A64-019.5 §9."""
    return TournamentMatchCompletionConsumer(
        build_advancement_service(
            session, matches=matches, settings=settings, events=events, clock=clock
        )
    )


class SessionScopedAttendance:
    """`TournamentAttendance` that opens a session per write — §6e.

    The same arrangement `game`'s `SessionScopedMatchRosters` uses and for
    the same reason: the caller is a WebSocket, whose "request" scope is the
    whole connection, and a dependency resolved through `DbSessionDep` would
    hold one PostgreSQL session per open socket for the length of a game.

    A join holds a connection for one guarded `UPDATE`, and commits it —
    unlike the roster read beside it, this one writes, and a room join is
    not inside anybody else's transaction.

    Holds only a session factory, so nothing per-call survives the method
    and one of these can live for the length of a connection.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def mark_present(self, match_id: UUID, player_id: UUID, *, at: datetime) -> bool:
        async with self._session_factory() as session:
            recorded = await SqlAlchemyPairingAttemptRepository(session).mark_present(
                match_id, player_id, at=at
            )
            await session.commit()
        return recorded


def get_tournament_results(session: DbSessionDep) -> SqlAlchemyTournamentResults:
    """The four public reads, over this request's session — §9–§12.

    A **read** adapter, deliberately distinct from the write repositories
    beside it: nothing it holds can take a lock, move a bracket or
    materialise a result, so a route cannot change a tournament by reading
    one. Request-scoped, because every one of the four is a bounded read
    that ends with the response.
    """
    return SqlAlchemyTournamentResults(session)


TournamentResultsDep = Annotated[SqlAlchemyTournamentResults, Depends(get_tournament_results)]


def get_tournament_directory(session: DbSessionDep) -> TournamentDirectory:
    """The lobby's one read, over this request's session — A64-020.0B.

    The same adapter as `get_tournament_results`, held through the
    **narrower** port: a route that lists tournaments needs one method, and
    typing it as the application protocol is what stops the other seven
    being reachable from the lobby handler. The concrete class is named here
    and nowhere above — the composition root is the only place presentation
    meets infrastructure.
    """
    return SqlAlchemyTournamentResults(session)


TournamentDirectoryDep = Annotated[TournamentDirectory, Depends(get_tournament_directory)]


def build_attendance(session: AsyncSession) -> TournamentAttendance:
    """The gateway's one inbound command, over a caller's session — §6e.

    The repository satisfies `tournament.public.TournamentAttendance`
    structurally, and nothing wider crosses: the transport tier can record
    that a player reached a match and can neither read a bracket nor move
    one.
    """
    return SqlAlchemyPairingAttemptRepository(session)


def build_notification_reader(session: AsyncSession) -> TournamentNotificationReader:
    """The fan-out reader `notifications` holds — A64-021.4 §6.

    Built per relay tick over that tick's session, like every other reader
    this module hands out. What crosses is two reads: the consumer can learn
    who is in a tournament and what they placed, and can change nothing.
    """
    return SqlAlchemyTournamentNotificationReader(session)


def get_attendance_ws(websocket: WebSocket) -> TournamentAttendance:
    """The gateway's attendance writer, for a WebSocket route.

    Reads the session **factory** off `app.state` rather than taking a
    resolved session — see `SessionScopedAttendance`. A socket-only variant
    because the only caller is a room join; if an HTTP route ever needs
    this, it takes `build_attendance` over its request session.
    """
    return SessionScopedAttendance(websocket.app.state.db.session_factory)


def build_no_show_service(
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    origin_matches: OriginMatchReader,
    settings: TournamentSettings,
    events: EventPublisher,
    clock: Clock,
) -> TournamentNoShowService:
    """The no-show sweep — §6e.

    Holds the **same** advancement service the outbox consumer does, so a
    real result that arrives while an attempt is claimed is applied through
    one path rather than two that could disagree about what it means.
    """
    return TournamentNoShowService(
        tournaments=SqlAlchemyTournamentRepository(session),
        bracket=SqlAlchemyBracketRepository(session),
        brackets=build_bracket_service(session, events=events, clock=clock),
        attempts=SqlAlchemyPairingAttemptRepository(session),
        advancement=build_advancement_service(
            session, matches=matches, settings=settings, events=events, clock=clock
        ),
        origin_matches=origin_matches,
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
        batch_size=settings.no_show_batch_size,
    )


def build_reconciliation_service(
    session: AsyncSession,
    *,
    matches: MatchCreationUseCase,
    settings: TournamentSettings,
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
        launcher=build_match_launcher(session, matches=matches, clock=clock, settings=settings),
        advancement=build_advancement_service(
            session, matches=matches, settings=settings, events=events, clock=clock
        ),
        unit_of_work=SessionUnitOfWork(session),
        clock=clock,
    )


WebSocketTournamentAttendanceDep = Annotated[TournamentAttendance, Depends(get_attendance_ws)]

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
    "build_attendance",
    "build_bracket_service",
    "build_completion_service",
    "build_deadline_service",
    "build_match_completion_consumer",
    "build_match_launcher",
    "build_notification_reader",
    "build_no_show_service",
    "WebSocketTournamentAttendanceDep",
    "TournamentDirectoryDep",
    "TournamentResultsDep",
    "get_attendance_ws",
    "get_tournament_directory",
    "get_tournament_results",
    "build_reconciliation_service",
    "build_registration_service",
    "build_seeding_service",
    "build_start_service",
    "build_tournament_reader",
    "get_bracket_service",
    "get_registration_service",
    "get_seeding_service",
]
