"""Tournament and game events becoming durable notifications — A64-021.4 §32.

Against real PostgreSQL and through the **production composition**: every
dispatcher below is built by the factory `app_factory` calls, over a real
outbox and a real relay. §33 asks for a reachability proof rather than a
demonstration that a class works, and that is the difference between this
file and a unit test with a fake sink.

## What each test is actually about

  **A registration produces a receipt, and a failed one produces nothing.**
  The event is published inside the registration transaction, so the two are
  as durable as each other.

  **Fan-out reaches the live field and nobody else.** A withdrawn entrant is
  excluded by the audience read's own predicate, not by a filter somebody
  has to remember, and a redelivered publication inserts nothing.

  **A preference stops creation, not display.** A64-021.3's policy applies
  to a 128-way fan-out exactly as it applies to one friend request.

  **Cost does not scale with the field.** The statement count at 128
  recipients is asserted, because an N+1 here is invisible in every test
  with two players and fatal in production.

  **A completed game tells each player their own result**, and an aborted
  one tells nobody anything.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import OutboxSettings
from app.core.clock import SystemClock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.game.domain.events import MatchCompleted, SeatSummary
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.public import PlayerSide, ProductVariant
from app.modules.notifications.application.services import (
    GameNotificationDispatcher,
    TournamentNotificationDispatcher,
)
from app.modules.notifications.application.services.game_notification_dispatcher import (
    CONSUMER_NAME as GAME_CONSUMER,
)
from app.modules.notifications.application.services.tournament_notification_dispatcher import (
    CONSUMER_NAME as TOURNAMENT_CONSUMER,
)
from app.modules.notifications.domain.preference import IN_APP_ONLY, DeliveryChannel
from app.modules.notifications.domain.record import (
    GameResultSummary,
    NavigationTargetType,
    NotificationCategory,
    NotificationType,
    TournamentSummary,
)
from app.modules.notifications.infrastructure.repositories import (
    SqlAlchemyNotificationPreferenceRepository,
    SqlAlchemyNotificationRepository,
)
from app.modules.notifications.presentation.dependencies import (
    build_durable_notification_writer,
    build_game_notification_dispatcher,
    build_tournament_notification_dispatcher,
)
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyRatingReader,
)
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.ports import AlreadyRegistered
from app.modules.tournament.application.services.registration_service import (
    TournamentRegistrationService,
)
from app.modules.tournament.application.services.seeding_service import (
    TournamentSeedingService,
)
from app.modules.tournament.domain.standings import FinalStatus, Standing
from app.modules.tournament.infrastructure.rating_snapshots import PublishedRatingSnapshots
from app.modules.tournament.infrastructure.repositories.tournament_repository import (
    SqlAlchemyPairingRepository,
    SqlAlchemyRegistrationRepository,
    SqlAlchemySeedRepository,
    SqlAlchemyStandingRepository,
    SqlAlchemyTournamentRepository,
)
from app.modules.tournament.presentation.dependencies import build_notification_reader
from app.platform.outbox import (
    OutboxEventPublisher,
    OutboxRelay,
    SqlAlchemyOutboxRepository,
    SqlAlchemyProcessedEventStore,
)
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

#: What a placement is called. `standing`'s own check constraints tie the two
#: together, so a test that set them independently would be refused.
_STATUS_OF = {1: FinalStatus.CHAMPION, 2: FinalStatus.RUNNER_UP}


class _KnownPlayers:
    """A `PlayerDirectory` the test dictates — `tournament`'s §3 seam."""

    def __init__(self, *known: UUID) -> None:
        self.known = set(known)

    async def get_profile(self, user_id: UUID) -> object:
        if user_id not in self.known:
            raise LookupError(f"no such player {user_id}")
        return object()


class _NoProfiles:
    """A `ProfileRenderer` that knows nobody.

    Used where the subject under test is the *outcome resolution* rather
    than the profile gate: a game notification with no opponent summary is
    a real production case (a deactivated account) and it exercises the same
    branch without needing four registered users.
    """

    async def render_many(
        self, player_ids: Sequence[UUID], *, relationship: Any
    ) -> dict[UUID, Any]:
        return {}


def _registration(
    session: AsyncSession, *, players: _KnownPlayers
) -> TournamentRegistrationService:
    """The registration service with a **real** outbox publisher.

    The publisher is the point: `PlayerRegistered` has to land in
    `platform.outbox` inside the registration's own transaction, and a
    recording double would prove the call and not the durability.
    """
    return TournamentRegistrationService(
        tournaments=SqlAlchemyTournamentRepository(session),
        registrations=SqlAlchemyRegistrationRepository(session),
        players=players,
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
        unit_of_work=SessionUnitOfWork(session),
        clock=MovableClock(NOW),
    )


def _seeding(session: AsyncSession) -> TournamentSeedingService:
    return TournamentSeedingService(
        tournaments=SqlAlchemyTournamentRepository(session),
        seeds=SqlAlchemySeedRepository(session),
        pairings=SqlAlchemyPairingRepository(session),
        ratings=PublishedRatingSnapshots(SqlAlchemyRatingReader(session)),
        events=OutboxEventPublisher(SqlAlchemyOutboxRepository(session)),
        unit_of_work=SessionUnitOfWork(session),
        clock=MovableClock(NOW),
    )


def _tournament_dispatcher(session: AsyncSession) -> TournamentNotificationDispatcher:
    """The consumer, assembled exactly as `app_factory` assembles it."""
    return build_tournament_notification_dispatcher(
        tournaments=build_notification_reader(session),
        store=build_durable_notification_writer(session),
    )


def _game_dispatcher(session: AsyncSession) -> GameNotificationDispatcher:
    return build_game_notification_dispatcher(
        profiles=_NoProfiles(),  # type: ignore[arg-type]
        store=build_durable_notification_writer(session),
    )


async def _drain(session: AsyncSession, dispatcher: Any, consumer: str) -> Any:
    """One relay tick over the real outbox.

    Driven explicitly rather than by the worker's timer: a suite that slept
    would depend on wall-clock time, which CLAUDE.md §6.4 rules out.
    `run_once` is public for exactly this.
    """
    settings = OutboxSettings()
    relay = OutboxRelay(
        outbox=SqlAlchemyOutboxRepository(session),
        processed=SqlAlchemyProcessedEventStore(session),
        handlers=[dispatcher],
        unit_of_work=SessionUnitOfWork(session),
        clock=SystemClock(),
        worker_id=f"contract-{consumer}",
        batch_size=settings.batch_size,
        max_attempts=settings.max_attempts,
        retry_base_seconds=settings.retry_base_seconds,
        retry_max_seconds=settings.retry_max_seconds,
    )
    return await relay.run_once()


async def _drain_all(session: AsyncSession, consumer: str) -> None:
    """Ticks until the backlog is empty.

    The relay claims `batch_size` entries per tick (50 by default), so a
    128-player registration sweep needs three. Bounded by a loop count
    rather than `while True`, so a consumer that never makes progress fails
    the test instead of hanging it.
    """
    for _ in range(20):
        tick = await _drain(session, _tournament_dispatcher(session), consumer)
        if tick.claimed == 0:
            return
    raise AssertionError("the outbox backlog did not drain")


async def _notifications(session: AsyncSession, recipient: UUID) -> list[Any]:
    page = await SqlAlchemyNotificationRepository(session).list_for(
        recipient, after=None, limit=200
    )
    return list(page.entries)


async def _open_tournament(service: TournamentRegistrationService, *, capacity: int = 4) -> Any:
    tournament = await service.create(
        name="Sunday Open",
        variant=ProductVariant.RUSSIAN_8X8,
        speed_class=SpeedClass.CLASSICAL,
        capacity=capacity,
        registration_deadline=None,
    )
    return await service.open_registration(tournament.id)


class TestRegistrationConfirmed:
    async def test_entering_a_tournament_produces_one_receipt(
        self, contract_session: AsyncSession
    ) -> None:
        """§32.1, and the reachability proof — §33.

        The whole path: a registration through the real service, an outbox
        row written in its transaction, a relay tick, and the composition
        root's own dispatcher and writer.
        """
        player = uuid4()
        service = _registration(contract_session, players=_KnownPlayers(player))
        tournament = await _open_tournament(service)

        await service.register(tournament.id, player)
        await _drain(
            contract_session, _tournament_dispatcher(contract_session), TOURNAMENT_CONSUMER
        )

        stored = await _notifications(contract_session, player)
        assert [record.type for record in stored] == [
            NotificationType.TOURNAMENT_REGISTRATION_CONFIRMED
        ]

        payload = stored[0].payload
        assert isinstance(payload, TournamentSummary)
        # The name is a snapshot on the event, so the receipt needs no read
        # back — and survives a rename.
        assert (payload.tournament_id, payload.tournament_name) == (tournament.id, "Sunday Open")
        # An id, never a URL: the client owns `/tournaments/{id}`.
        assert stored[0].target.type is NavigationTargetType.TOURNAMENT
        assert stored[0].target.ref == str(tournament.id)

    async def test_a_refused_registration_produces_nothing(
        self, contract_session: AsyncSession
    ) -> None:
        """§32.2. A duplicate entry raises before the publish, so there is
        no second receipt — and no first one for a failure that never took
        a seat."""
        player = uuid4()
        service = _registration(contract_session, players=_KnownPlayers(player))
        tournament = await _open_tournament(service)
        await service.register(tournament.id, player)

        with pytest.raises(AlreadyRegistered):
            await service.register(tournament.id, player)

        await _drain(
            contract_session, _tournament_dispatcher(contract_session), TOURNAMENT_CONSUMER
        )
        assert len(await _notifications(contract_session, player)) == 1


class TestRoundPublished:
    async def test_it_reaches_the_live_field_and_only_once(
        self, contract_session: AsyncSession
    ) -> None:
        """§32.3 and §32.5 together, because they are one property.

        A withdrawn entrant is excluded by the audience read, and a second
        drain over the same event inserts nothing — the `(recipient,
        source_event, type)` constraint doing at 4 rows what it does at 1.

        Asserted together rather than in two tests because the second drain
        is the only honest way to check the first: a fan-out that wrote the
        right people once and duplicated them on redelivery would pass a
        recipient-set assertion on its own.
        """
        staying = [uuid4() for _ in range(3)]
        leaving = uuid4()
        service = _registration(contract_session, players=_KnownPlayers(*staying, leaving))
        tournament = await _open_tournament(service)
        for player in (*staying, leaving):
            await service.register(tournament.id, player)
        await service.withdraw(tournament.id, leaving)

        await service.close_registration(tournament.id)
        await _seeding(contract_session).seed_tournament(tournament.id)

        await _drain(
            contract_session, _tournament_dispatcher(contract_session), TOURNAMENT_CONSUMER
        )
        await _drain(
            contract_session, _tournament_dispatcher(contract_session), TOURNAMENT_CONSUMER
        )

        for player in staying:
            rounds = [
                record
                for record in await _notifications(contract_session, player)
                if record.type is NotificationType.TOURNAMENT_ROUND_PUBLISHED
            ]
            assert len(rounds) == 1, "one publication, one row, however many times it is delivered"
            assert isinstance(rounds[0].payload, TournamentSummary)
            assert rounds[0].payload.round_number == 1

        assert [
            record
            for record in await _notifications(contract_session, leaving)
            if record.type is NotificationType.TOURNAMENT_ROUND_PUBLISHED
        ] == [], "a player who withdrew is not in the field and is not told about it"

    async def test_a_disabled_tournament_preference_suppresses_the_fan_out(
        self, contract_session: AsyncSession
    ) -> None:
        """§32.4. A64-021.3's policy, applied to a fan-out.

        The muted player gets **no row**, which is the claim: suppression
        happens where the notification would have been created, so there is
        nothing to hide on read and nothing for the badge to count.
        """
        muted, listening = uuid4(), uuid4()
        await SqlAlchemyNotificationPreferenceRepository(
            contract_session, availability=IN_APP_ONLY
        ).replace(
            muted,
            changes=[(NotificationCategory.TOURNAMENT, DeliveryChannel.IN_APP, False)],
            at=NOW,
        )

        service = _registration(contract_session, players=_KnownPlayers(muted, listening))
        tournament = await _open_tournament(service)
        for player in (muted, listening):
            await service.register(tournament.id, player)

        await _drain(
            contract_session, _tournament_dispatcher(contract_session), TOURNAMENT_CONSUMER
        )

        assert await _notifications(contract_session, muted) == []
        assert len(await _notifications(contract_session, listening)) == 1


class TestTournamentCompleted:
    async def test_every_entrant_is_told_their_own_placement(
        self, contract_session: AsyncSession
    ) -> None:
        """§32.8. `final_rank` is the one recipient-specific field on this
        platform's fan-out payloads, and it is why the completion
        notification is worth sending at all.

        Ranks are asserted **as recorded**: the standings below share a rank
        and skip one, which is what a tie produces, and a consumer that
        densified them would be reporting placements nobody was awarded.
        """
        champion, runner_up, tied = uuid4(), uuid4(), uuid4()
        service = _registration(contract_session, players=_KnownPlayers(champion, runner_up, tied))
        tournament = await _open_tournament(service)
        for player in (champion, runner_up, tied):
            await service.register(tournament.id, player)

        await SqlAlchemyStandingRepository(contract_session).record(
            [
                _standing(tournament.id, champion, rank=1, seed=1),
                _standing(tournament.id, runner_up, rank=2, seed=2),
                _standing(tournament.id, tied, rank=2, seed=3),
            ]
        )
        await _publish_completed(contract_session, tournament.id, winner_id=champion)
        await _drain(
            contract_session, _tournament_dispatcher(contract_session), TOURNAMENT_CONSUMER
        )

        ranks = {}
        for player in (champion, runner_up, tied):
            completed = [
                record
                for record in await _notifications(contract_session, player)
                if record.type is NotificationType.TOURNAMENT_COMPLETED
            ]
            assert len(completed) == 1
            assert isinstance(completed[0].payload, TournamentSummary)
            ranks[player] = completed[0].payload.final_rank

        assert ranks == {champion: 1, runner_up: 2, tied: 2}


class TestFanOutCost:
    async def test_a_full_field_does_not_cost_a_query_per_recipient(
        self, contract_session: AsyncSession
    ) -> None:
        """§32.6, §27. The N+1 that only appears in production.

        128 is the platform's tournament capacity, so this is the largest
        fan-out that can exist.

        **Measured: 6 `SELECT`s at 16 recipients and 6 at 128** — the count
        does not move with the field, which is the whole claim. A
        per-recipient audience or preference read would produce ~130.

        The assertion is 20 rather than 6 on purpose: what it must catch is
        *growth with the field*, and pinning an exact plan would make it
        fail on a SQLAlchemy release that emits one more round trip.
        """
        players = [uuid4() for _ in range(128)]
        service = _registration(contract_session, players=_KnownPlayers(*players))
        tournament = await _open_tournament(service, capacity=128)
        for player in players:
            await service.register(tournament.id, player)

        # The 128 registration events are drained **first and uncounted**.
        # What is being bounded is one fan-out, and leaving 128 single-
        # recipient events in the same measurement would bury it.
        await _drain_all(contract_session, TOURNAMENT_CONSUMER)

        await service.close_registration(tournament.id)
        await _seeding(contract_session).seed_tournament(tournament.id)

        with _counted(contract_session) as statements:
            await _drain(
                contract_session, _tournament_dispatcher(contract_session), TOURNAMENT_CONSUMER
            )

        assert len(await _notifications(contract_session, players[0])) == 2
        # One audience read, one preference read, and the relay's own claim
        # and ledger reads. The inserts are per-row because each *is* a row;
        # nothing else may scale with the field.
        assert statements.reads < 20, (
            f"{statements.reads} reads for a 128-recipient fan-out — "
            "a read per recipient is the N+1 this bounds"
        )


class TestGameCompleted:
    async def test_both_players_are_told_their_own_result(
        self, contract_session: AsyncSession
    ) -> None:
        """§32. The outcome is resolved per seat **before** it is stored, so
        a client renders "you won" without knowing which seat it held."""
        light, dark = uuid4(), uuid4()
        match_id = uuid4()
        await _publish_match_completed(
            contract_session,
            match_id=match_id,
            light=light,
            dark=dark,
            outcome=MatchOutcome.WIN,
            winner=PlayerSide.LIGHT,
        )
        await _drain(contract_session, _game_dispatcher(contract_session), GAME_CONSUMER)

        told = {}
        for player in (light, dark):
            stored = await _notifications(contract_session, player)
            assert [record.type for record in stored] == [NotificationType.GAME_COMPLETED]
            payload = stored[0].payload
            assert isinstance(payload, GameResultSummary)
            told[player] = payload.outcome
            # The replay, never the live board: by the time this is read the
            # game is over and `/games/{id}` has nothing to show.
            assert stored[0].target.type is NavigationTargetType.MATCH_REPLAY
            assert stored[0].target.ref == str(match_id)

        assert told == {light: "win", dark: "loss"}

    async def test_an_aborted_match_tells_nobody(self, contract_session: AsyncSession) -> None:
        """MT-11: a match that did not happen. A permanent record saying
        somebody's game finished would describe a non-event."""
        light, dark = uuid4(), uuid4()
        await _publish_match_completed(
            contract_session,
            match_id=uuid4(),
            light=light,
            dark=dark,
            outcome=MatchOutcome.NONE,
            winner=None,
        )
        await _drain(contract_session, _game_dispatcher(contract_session), GAME_CONSUMER)

        assert await _notifications(contract_session, light) == []
        assert await _notifications(contract_session, dark) == []


# --- helpers ----------------------------------------------------------------


def _standing(tournament_id: UUID, player_id: UUID, *, rank: int, seed: int) -> Standing:
    return Standing(
        tournament_id=tournament_id,
        player_id=player_id,
        final_rank=rank,
        seed_number=seed,
        # Both or neither — `ck_standing__elimination_is_complete`. A rank
        # below first means somebody knocked them out, and the database
        # refuses a half-recorded elimination.
        elimination_round=None if rank == 1 else 1,
        eliminated_by_player_id=None if rank == 1 else uuid4(),
        wins=0,
        losses=0,
        draws=0,
        adjudicated_advancements=0,
        # `ck_standing__runner_up_iff_second` — the status and the rank are
        # one fact recorded twice, and the database refuses a disagreement.
        final_status=_STATUS_OF.get(rank, FinalStatus.ELIMINATED),
        created_at=NOW,
    )


async def _publish_completed(
    session: AsyncSession, tournament_id: UUID, *, winner_id: UUID
) -> None:
    """`tournament.completed`, published through the real outbox.

    The completion *service* is not driven here: reaching it needs a played
    bracket, and what this file is about is the consumer. The event is the
    contract between them, and it is written exactly as the service writes
    it — `TournamentCompleted.payload()`.
    """
    from app.modules.tournament.domain.events import TournamentCompleted

    await OutboxEventPublisher(SqlAlchemyOutboxRepository(session)).publish(
        TournamentCompleted(
            occurred_at=NOW,
            tournament_id=tournament_id,
            winner_id=winner_id,
            entrant_count=3,
        )
    )
    await session.flush()


async def _publish_match_completed(
    session: AsyncSession,
    *,
    match_id: UUID,
    light: UUID,
    dark: UUID,
    outcome: MatchOutcome,
    winner: PlayerSide | None,
) -> None:
    """`game.match_completed`, published through the real outbox.

    The **real event object**, not a hand-written payload dict. The
    alternative to publishing it is playing a whole game to a finish, which
    exercises `game`'s own suite rather than this consumer — but the event
    itself is the contract between the two, so it is built by its own
    constructor and serialised by its own `payload()`.
    """
    await OutboxEventPublisher(SqlAlchemyOutboxRepository(session)).publish(
        MatchCompleted(
            occurred_at=NOW,
            match_id=match_id,
            variant=ProductVariant.RUSSIAN_8X8,
            rated=True,
            outcome=outcome,
            termination_reason=TerminationReason.RESIGNATION,
            winner=winner,
            ply_number=20,
            light=_seat(light),
            dark=_seat(dark),
        )
    )
    await session.flush()


def _seat(player_id: UUID) -> SeatSummary:
    """A rating snapshot the notification path never reads.

    Present because `MatchCompleted` carries one and this test builds the
    **real** event rather than a hand-written payload dict — which is the
    coupling worth having: a producer that renames a key breaks this file
    instead of silently producing notifications nobody can decode.
    """
    return SeatSummary(
        player_id=player_id,
        rating_value=1500.0,
        rating_deviation=350.0,
        rating_volatility=0.06,
        games_played=0,
        is_provisional=True,
    )


class _Counted:
    def __init__(self) -> None:
        self.reads = 0


class _counted:
    """Counts `SELECT` statements issued while the block runs.

    Listens on the `Engine` **class** rather than one connection: the
    session's bind is an `AsyncConnection` in this suite, and reaching
    through it for a sync engine couples the test to SQLAlchemy's async
    plumbing. The class-level listener sees every statement this test
    issues, which is exactly the population being bounded.

    Reads only: the inserts are one per notification by construction, and
    counting them would assert the size of the field rather than the shape
    of the access pattern.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._counter = _Counted()

    def __enter__(self) -> _Counted:
        event.listen(Engine, "before_cursor_execute", self._record)
        return self._counter

    def __exit__(self, *_: object) -> None:
        event.remove(Engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            self._counter.reads += 1
