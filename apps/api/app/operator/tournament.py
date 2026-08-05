"""Operator commands for the tournament lifecycle — A64-019.8 §3, §5.

    python -m app.operator.tournament create  --name … --capacity 8
    python -m app.operator.tournament open    <tournament-id>
    python -m app.operator.tournament close   <tournament-id>
    python -m app.operator.tournament seed    <tournament-id>
    python -m app.operator.tournament start   <tournament-id>
    python -m app.operator.tournament run     <tournament-id>

Five commands and one composite. Each **reuses the existing application
service** — this module resolves a session, calls one use case and prints
what happened. There is no orchestration here and no validation here: the
capacity bounds are `Tournament.__post_init__`'s, the transitions are
`_ALLOWED`'s, and seeding's idempotency is the primary key's.

See `app/operator/__init__.py` for why these are a process profile rather
than `/api/v1/admin` routes.

## Idempotency

Every command is safe to re-run, and each for a reason that already
existed:

    create  not idempotent, and says so — it mints a new id. The only
            command here that is not, because there is no key to be
            idempotent *on*: two tournaments with one name are two
            tournaments
    open    the aggregate refuses a second transition; the command reports
            the current state instead of failing
    close   the same, and it converges with `TournamentDeadlineTask` —
            whichever gets there first wins and the other reports
    seed     returns the persisted plan unchanged (A64-019.3 §11)
    start    launches only what is missing (A64-019.5 §8)

## Exit codes

`0` for success and for a no-op that reported the current state; `1` for a
refusal the operator has to act on. Errors are printed as one line —
nothing here is parsed by a machine, and a traceback in an operator's
terminal is noise around the sentence that matters.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.core.clock import SystemClock
from app.core.exceptions import Arena64Error
from app.database.session_manager import DatabaseSessionManager
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.public import MatchCreationUseCase, ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.services.registration_service import (
    TournamentRegistrationService,
)
from app.modules.tournament.domain.tournament import Tournament, TournamentStatus
from app.modules.tournament.presentation.dependencies import (
    build_registration_service,
    build_seeding_service,
    build_start_service,
    build_tournament_reader,
)
from app.modules.users.application.services.user_service import UserService
from app.modules.users.infrastructure.repositories.user_repository import (
    SqlAlchemyUserRepository,
)
from app.modules.users.public import UserProfileService
from app.platform.outbox import OutboxEventPublisher, SqlAlchemyOutboxRepository

logger = logging.getLogger(__name__)


async def create(
    session: AsyncSession,
    settings: Settings,
    *,
    name: str,
    variant: ProductVariant,
    capacity: int,
    rated: bool,
    registration_deadline: datetime | None,
    created_by: UUID | None,
) -> Tournament:
    """Creates a tournament in `DRAFT` — §4.

    `format` and `speed_class` are **not** parameters. v0.x runs one format
    and seeds on one speed class, and accepting either would let an
    operator ask for a tournament the aggregate then refuses — a worse
    error, later, with a row already written. The aggregate still checks,
    so this is a narrower door rather than a second rule.

    `created_by` is the operator's own account id when they supply one, and
    `None` for a system tournament. It is never read from anything a client
    sent: there is no client here.
    """
    return await _registration(session, settings).create(
        name=name,
        variant=variant,
        speed_class=SpeedClass.CLASSICAL,
        capacity=capacity,
        rated=rated,
        created_by=created_by,
        registration_deadline=registration_deadline,
    )


async def open_registration(
    session: AsyncSession, settings: Settings, tournament_id: UUID
) -> Tournament:
    """`DRAFT` → `REGISTRATION_OPEN`, or reports the current state.

    Idempotent by re-read rather than by a flag: a tournament that is
    already open is the outcome the operator wanted, and turning that into
    a failure would make a retried command look like a broken one.
    """
    return await _transition_once(
        session, settings, tournament_id, TournamentStatus.REGISTRATION_OPEN
    )


async def close_registration(
    session: AsyncSession, settings: Settings, tournament_id: UUID
) -> Tournament:
    """`REGISTRATION_OPEN` → `REGISTRATION_CLOSED`, or reports it.

    **Converges with `TournamentDeadlineTask`.** Both end in the same
    state, both take the row lock, and the aggregate's transition table
    refuses the second — so an operator closing a tournament the sweep is
    closing at the same instant sees "already closed" rather than a
    corrupted one.
    """
    return await _transition_once(
        session, settings, tournament_id, TournamentStatus.REGISTRATION_CLOSED
    )


async def seed(session: AsyncSession, settings: Settings, tournament_id: UUID) -> int:
    """Seeds a closed tournament and plans round one. Returns the slot count.

    Reuses `TournamentSeedingService`, so a second run returns the
    **persisted** plan rather than recomputing one — seeds are written once
    and a later phase must never re-derive them from current ratings
    (A64-019.3 §4).
    """
    plan = await build_seeding_service(
        session, events=_events(session), clock=SystemClock()
    ).seed_tournament(tournament_id)
    return len(plan)


async def start(session: AsyncSession, settings: Settings, tournament_id: UUID) -> int:
    """Starts a tournament and returns how many matches exist for round one.

    Reuses `TournamentStartService`, which materialises the bracket if it
    has not been, moves the tournament to `IN_PROGRESS`, and creates one
    `game` match per node that needs one — skipping byes, stamping
    `origin = TOURNAMENT` and `origin_ref = pairing.id`, leaving the queue
    ticket ids null, and carrying the seat rating snapshots.

    Idempotent: a second run launches only what is missing.
    """
    attempts = await build_start_service(
        session,
        matches=_match_creation(session),
        settings=settings.tournament,
        events=_events(session),
        clock=SystemClock(),
    ).start_tournament(tournament_id)
    return len(attempts)


async def _transition_once(
    session: AsyncSession,
    settings: Settings,
    tournament_id: UUID,
    target: TournamentStatus,
) -> Tournament:
    """One lifecycle move, or the current state if it has already been made.

    The re-read is deliberate rather than a swallowed error: the aggregate
    is the only thing that decides whether a transition is legal, so this
    asks it, and only treats *this exact target* as already-reached.
    Anything else propagates — an operator trying to open a completed
    tournament must hear about it.
    """
    current = await build_tournament_reader(session).by_id(tournament_id)
    if current is not None and current.status is target:
        return current

    service = _registration(session, settings)

    if target is TournamentStatus.REGISTRATION_OPEN:
        return await service.open_registration(tournament_id)
    return await service.close_registration(tournament_id)


def _registration(session: AsyncSession, settings: Settings) -> TournamentRegistrationService:
    """The registration use cases over this command's session.

    `UserProfileService` is assembled here rather than resolved through
    `Depends`, because there is no request: the same class the HTTP path
    uses, over the same session, so an operator and a player reach one
    graph.
    """
    return build_registration_service(
        session,
        players=UserProfileService(
            UserService(
                users=SqlAlchemyUserRepository(session),
                unit_of_work=SessionUnitOfWork(session),
                clock=SystemClock(),
            )
        ),
        events=_events(session),
        clock=SystemClock(),
    )


def _events(session: AsyncSession) -> OutboxEventPublisher:
    """The outbox over the **caller's** session, so a lifecycle event lands
    in the transaction that caused it (AD-16)."""
    return OutboxEventPublisher(SqlAlchemyOutboxRepository(session))


def _match_creation(session: AsyncSession) -> MatchCreationUseCase:
    """`game`'s command port, named here for the reason `app_factory` names
    it: composing across module lines is a composition root's job, and this
    module is one."""
    from app.modules.matchmaking.presentation.dependencies import build_match_creation

    return build_match_creation(session, events=_events(session), clock=SystemClock())


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.operator.tournament",
        description="Tournament lifecycle commands for an operator.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("create", help="Create a tournament in DRAFT.")
    new.add_argument("--name", required=True)
    new.add_argument("--capacity", type=int, required=True, help="Between 2 and 128.")
    new.add_argument(
        "--variant",
        default=ProductVariant.RUSSIAN_8X8.value,
        choices=[member.value for member in ProductVariant],
    )
    new.add_argument("--unrated", action="store_true", help="Create a casual tournament.")
    new.add_argument(
        "--registration-deadline",
        default=None,
        help="ISO-8601 instant at which registration closes on its own.",
    )
    new.add_argument("--created-by", default=None, help="The operator's own account id.")

    for name, help_text in (
        ("open", "DRAFT -> REGISTRATION_OPEN."),
        ("close", "REGISTRATION_OPEN -> REGISTRATION_CLOSED."),
        ("seed", "Seed a closed tournament and plan round one."),
        ("start", "Materialise the bracket and create round one's matches."),
        ("run", "close, seed and start, in order."),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("tournament_id")

    return parser


async def _dispatch(arguments: argparse.Namespace) -> str:
    """Runs one command over one session, and returns the line to print."""
    settings = get_settings()
    database = DatabaseSessionManager(settings.postgres)
    try:
        async with database.session_factory() as session:
            return await _run(arguments, session, settings)
    finally:
        await database.close()


async def _run(arguments: argparse.Namespace, session: AsyncSession, settings: Settings) -> str:
    if arguments.command == "create":
        tournament = await create(
            session,
            settings,
            name=arguments.name,
            variant=ProductVariant(arguments.variant),
            capacity=arguments.capacity,
            rated=not arguments.unrated,
            registration_deadline=(
                datetime.fromisoformat(arguments.registration_deadline)
                if arguments.registration_deadline
                else None
            ),
            created_by=UUID(arguments.created_by) if arguments.created_by else None,
        )
        return f"created {tournament.id} ({tournament.status.value})"

    tournament_id = UUID(arguments.tournament_id)
    handlers: dict[str, Callable[[], Awaitable[str]]] = {
        "open": lambda: _describe(open_registration(session, settings, tournament_id)),
        "close": lambda: _describe(close_registration(session, settings, tournament_id)),
        "seed": lambda: _count("seeded", seed(session, settings, tournament_id), "slots"),
        "start": lambda: _count("started", start(session, settings, tournament_id), "matches"),
        "run": lambda: _run_all(session, settings, tournament_id),
    }
    return await handlers[arguments.command]()


async def _run_all(session: AsyncSession, settings: Settings, tournament_id: UUID) -> str:
    """`close`, `seed`, `start` — the three steps that always follow each
    other, so an operator running a tournament types one command.

    Each step is the same idempotent use case the individual commands call,
    so re-running this after a partial failure resumes rather than repeats.
    """
    await close_registration(session, settings, tournament_id)
    slots = await seed(session, settings, tournament_id)
    matches = await start(session, settings, tournament_id)
    return f"running {tournament_id}: {slots} slots seeded, {matches} matches created"


async def _describe(awaitable: Awaitable[Tournament]) -> str:
    tournament = await awaitable
    return f"{tournament.id} is {tournament.status.value}"


async def _count(verb: str, awaitable: Awaitable[int], noun: str) -> str:
    return f"{verb}: {await awaitable} {noun}"


def main(argv: list[str] | None = None) -> int:
    """The process entry point. Returns an exit code rather than raising.

    A refusal is a sentence and a `1`, never a traceback: an operator
    reading a terminal at 3am needs the reason, and the stack that produced
    an `InvalidTournamentTransition` says nothing they can act on. Anything
    that is *not* an `Arena64Error` is a defect and does propagate, because
    it is not a refusal and a tidy message would hide it.
    """
    settings = get_settings()
    configure_logging(level=settings.app.log_level, environment=settings.environment)
    arguments = _parser().parse_args(argv)

    try:
        print(asyncio.run(_dispatch(arguments)))  # noqa: T201 — an operator's terminal
    except Arena64Error as refused:
        print(f"refused: {refused}", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["close_registration", "create", "main", "open_registration", "seed", "start"]
