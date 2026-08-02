"""The acceptance handshake — A64-015.4 §6.

`MatchRecord`'s guards and `MatchAcceptanceService`'s orchestration, in one
file because every rule here is one thing said twice: the aggregate refuses
a transition and the service turns that refusal into an outcome. Splitting
them would duplicate the scenario table without adding a case.

What is substituted is storage and the clock (`tests/fakes/matches.py`);
the service, the aggregate and the events are all real, so the transaction
boundary, the compare-and-set and the "which event, when" decision are
genuinely exercised.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION, PlayerSide
from app.modules.game.application.services import MatchAcceptanceService
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus, MatchSeat
from app.modules.game.public import (
    AcceptanceWindowClosed,
    MatchNotFound,
    MatchNotPending,
    ProductVariant,
)
from tests.fakes.matches import InMemoryMatchRecordRepository
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import RecordingPublisher

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

#: The default `MATCHMAKING_RESERVATION_TTL_SECONDS`. One number, and the
#: same one both queue tickets carry as `reserved_until` — see
#: `game.domain.match_record` on why the two are the same instant rather
#: than two timers.
WINDOW = timedelta(seconds=30)


def _record(
    *,
    light: UUID | None = None,
    dark: UUID | None = None,
    at: datetime = NOW,
) -> MatchRecord:
    """A freshly created, unanswered match."""
    return MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(player_id=light or generate_uuid7(), queue_ticket_id=generate_uuid7()),
        dark=MatchSeat(player_id=dark or generate_uuid7(), queue_ticket_id=generate_uuid7()),
        created_at=at,
        acceptance_deadline=at + WINDOW,
    )


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def matches() -> InMemoryMatchRecordRepository:
    return InMemoryMatchRecordRepository()


@pytest.fixture
def events() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def service(
    matches: InMemoryMatchRecordRepository, events: RecordingPublisher, clock: MovableClock
) -> MatchAcceptanceService:
    return MatchAcceptanceService(
        matches=matches,
        events=events,
        unit_of_work=NullUnitOfWork(),
        clock=clock,
    )


async def _stored(matches: InMemoryMatchRecordRepository, record: MatchRecord) -> MatchRecord:
    stored, _ = await matches.create(record)
    return stored


class TestANewlyPairedMatchIsNotActive:
    """§4: a match must not become `ACTIVE` until acceptance succeeds."""

    def test_a_created_match_is_pending(self) -> None:
        assert _record().status is MatchRecordStatus.PENDING_ACCEPTANCE

    def test_neither_side_has_answered(self) -> None:
        record = _record()

        assert not record.light.has_accepted
        assert not record.dark.has_accepted

    def test_a_record_claiming_to_be_active_without_two_answers_is_refused(self) -> None:
        """The invariant stated on the aggregate as well as in the CHECK, so
        a row rehydrated from a repair script fails at the boundary rather
        than reaching a response."""
        with pytest.raises(ValueError, match="accepted by both"):
            MatchRecord(
                pairing_id=generate_uuid7(),
                variant=ProductVariant.RUSSIAN_8X8,
                rated=True,
                engine_version=CURRENT_ENGINE_VERSION,
                light=MatchSeat(player_id=generate_uuid7(), queue_ticket_id=generate_uuid7()),
                dark=MatchSeat(player_id=generate_uuid7(), queue_ticket_id=generate_uuid7()),
                created_at=NOW,
                acceptance_deadline=NOW + WINDOW,
                status=MatchRecordStatus.ACTIVE,
                settled_at=NOW,
            )


class TestBothAcceptancesActivate:
    async def test_one_acceptance_leaves_the_match_pending(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        record = await _stored(matches, _record())

        view = await service.accept(player_id=record.light.player_id, match_id=record.id)

        assert view.status is MatchRecordStatus.PENDING_ACCEPTANCE
        assert view.you_accepted
        assert not view.opponent_accepted

    async def test_both_acceptances_activate_the_match(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        record = await _stored(matches, _record())

        await service.accept(player_id=record.light.player_id, match_id=record.id)
        view = await service.accept(player_id=record.dark.player_id, match_id=record.id)

        assert view.status is MatchRecordStatus.ACTIVE
        assert view.you_accepted
        assert view.opponent_accepted

    async def test_activation_publishes_one_event_per_transition(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        events: RecordingPublisher,
    ) -> None:
        """`match_accepted_by_player` while the opponent is silent, then
        `match_activated` — never both for one answer, or every consumer of
        the first has to check whether the second is about to follow."""
        record = await _stored(matches, _record())

        await service.accept(player_id=record.light.player_id, match_id=record.id)
        await service.accept(player_id=record.dark.player_id, match_id=record.id)

        assert events.types() == ["game.match_accepted_by_player", "game.match_activated"]

    async def test_the_order_of_the_two_answers_does_not_matter(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        record = await _stored(matches, _record())

        await service.accept(player_id=record.dark.player_id, match_id=record.id)
        view = await service.accept(player_id=record.light.player_id, match_id=record.id)

        assert view.status is MatchRecordStatus.ACTIVE


class TestOneDeclinePreventsActivation:
    async def test_a_decline_cancels_the_match(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        record = await _stored(matches, _record())

        view = await service.decline(player_id=record.light.player_id, match_id=record.id)

        assert view.status is MatchRecordStatus.CANCELLED

    async def test_a_decline_after_the_opponent_accepted_still_cancels(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        """One decline ends it, whatever the other side did — a match two
        people were offered and one refused is not a match."""
        record = await _stored(matches, _record())
        await service.accept(player_id=record.light.player_id, match_id=record.id)

        view = await service.decline(player_id=record.dark.player_id, match_id=record.id)

        assert view.status is MatchRecordStatus.CANCELLED
        assert view.opponent_accepted

    async def test_the_accepting_player_cannot_activate_afterwards(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        """The race §14 names: a decline arriving before the second
        acceptance wins, and the late acceptance is refused rather than
        resurrecting a cancelled match."""
        record = await _stored(matches, _record())
        await service.decline(player_id=record.light.player_id, match_id=record.id)

        with pytest.raises(MatchNotPending):
            await service.accept(player_id=record.dark.player_id, match_id=record.id)

    async def test_a_decline_publishes_one_event(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        events: RecordingPublisher,
    ) -> None:
        record = await _stored(matches, _record())

        await service.decline(player_id=record.dark.player_id, match_id=record.id)

        assert events.types() == ["game.match_declined"]


class TestDuplicateAcceptanceIsSafe:
    async def test_accepting_twice_returns_the_same_state(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        """A client retrying after a dropped response asked for something
        already true; telling it otherwise makes a network blip look like a
        lost game."""
        record = await _stored(matches, _record())

        first = await service.accept(player_id=record.light.player_id, match_id=record.id)
        second = await service.accept(player_id=record.light.player_id, match_id=record.id)

        assert second == first

    async def test_accepting_twice_publishes_once(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        events: RecordingPublisher,
    ) -> None:
        record = await _stored(matches, _record())

        await service.accept(player_id=record.light.player_id, match_id=record.id)
        await service.accept(player_id=record.light.player_id, match_id=record.id)

        assert events.types() == ["game.match_accepted_by_player"]

    async def test_a_repeat_after_activation_cannot_un_activate_the_match(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        """The dangerous half of idempotency: a retry arriving after the
        opponent's acceptance must not reopen a match that has started."""
        record = await _stored(matches, _record())
        await service.accept(player_id=record.light.player_id, match_id=record.id)
        await service.accept(player_id=record.dark.player_id, match_id=record.id)

        view = await service.accept(player_id=record.light.player_id, match_id=record.id)

        assert view.status is MatchRecordStatus.ACTIVE

    async def test_declining_twice_is_refused(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        """Not symmetric with acceptance, deliberately: by the second call
        the match is cancelled and there is nothing left to refuse."""
        record = await _stored(matches, _record())
        await service.decline(player_id=record.light.player_id, match_id=record.id)

        with pytest.raises(MatchNotPending):
            await service.decline(player_id=record.light.player_id, match_id=record.id)


class TestOnlyAParticipantMayRespond:
    async def test_a_stranger_cannot_accept(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        """`MatchNotFound`, not a permission error: "that match is not
        yours" and "there is no such match" must be indistinguishable, or
        live match identifiers become enumerable."""
        record = await _stored(matches, _record())

        with pytest.raises(MatchNotFound):
            await service.accept(player_id=generate_uuid7(), match_id=record.id)

    async def test_a_stranger_cannot_decline(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        record = await _stored(matches, _record())

        with pytest.raises(MatchNotFound):
            await service.decline(player_id=generate_uuid7(), match_id=record.id)

    async def test_a_stranger_changes_nothing(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        events: RecordingPublisher,
    ) -> None:
        record = await _stored(matches, _record())

        with pytest.raises(MatchNotFound):
            await service.decline(player_id=generate_uuid7(), match_id=record.id)

        assert matches.matches[record.id].status is MatchRecordStatus.PENDING_ACCEPTANCE
        assert events.published == []

    async def test_a_player_cannot_answer_for_the_opponent(self) -> None:
        """There is no side parameter anywhere, so this is checked at the
        one place a side is decided: the caller's own identifier."""
        record = _record()

        assert record.side_of(record.light.player_id) is PlayerSide.LIGHT
        assert record.side_of(record.dark.player_id) is PlayerSide.DARK

    async def test_an_unknown_match_is_not_found(self, service: MatchAcceptanceService) -> None:
        with pytest.raises(MatchNotFound):
            await service.accept(player_id=generate_uuid7(), match_id=generate_uuid7())


class TestAnswersAfterTheDeadline:
    async def test_an_acceptance_after_the_deadline_is_refused(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        clock: MovableClock,
    ) -> None:
        """Refused by the *deadline*, not by the reconciler having run: the
        instant is the rule and the sweep is only the bookkeeping."""
        record = await _stored(matches, _record())
        clock.advance(WINDOW.total_seconds() + 1)

        with pytest.raises(AcceptanceWindowClosed):
            await service.accept(player_id=record.light.player_id, match_id=record.id)

    async def test_a_decline_after_the_deadline_is_refused(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        clock: MovableClock,
    ) -> None:
        record = await _stored(matches, _record())
        clock.advance(WINDOW.total_seconds() + 1)

        with pytest.raises(AcceptanceWindowClosed):
            await service.decline(player_id=record.light.player_id, match_id=record.id)

    async def test_an_answer_exactly_on_the_deadline_is_still_accepted(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        clock: MovableClock,
    ) -> None:
        """Non-strict on the boundary, so a client whose clock agrees with
        the server's to the microsecond is not refused for being punctual."""
        record = await _stored(matches, _record())
        clock.advance(WINDOW.total_seconds())

        view = await service.accept(player_id=record.light.player_id, match_id=record.id)

        assert view.you_accepted

    async def test_a_late_answer_leaves_the_match_untouched(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        events: RecordingPublisher,
        clock: MovableClock,
    ) -> None:
        record = await _stored(matches, _record())
        clock.advance(WINDOW.total_seconds() + 1)

        with pytest.raises(AcceptanceWindowClosed):
            await service.accept(player_id=record.light.player_id, match_id=record.id)

        assert matches.matches[record.id] == record
        assert events.published == []


class TestReadingYourPendingMatch:
    async def test_a_player_with_no_pending_match_reads_none(
        self, service: MatchAcceptanceService
    ) -> None:
        assert await service.pending_match(generate_uuid7()) is None

    async def test_each_participant_sees_the_match_from_their_own_seat(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        """Named from the reader's seat rather than by side, so a route
        cannot render the wrong half by picking the wrong field."""
        record = await _stored(matches, _record())
        await service.accept(player_id=record.light.player_id, match_id=record.id)

        light = await service.pending_match(record.light.player_id)
        dark = await service.pending_match(record.dark.player_id)

        assert light is not None and dark is not None
        assert light.you_accepted and not light.opponent_accepted
        assert dark.opponent_accepted and not dark.you_accepted
        assert light.opponent_player_id == record.dark.player_id
        assert dark.opponent_player_id == record.light.player_id

    async def test_a_settled_match_is_no_longer_pending(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        record = await _stored(matches, _record())
        await service.decline(player_id=record.light.player_id, match_id=record.id)

        assert await service.pending_match(record.light.player_id) is None

    async def test_the_view_carries_no_pairing_internals(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        """§7 forbids exposing pairing internals, and the way that is
        enforced is the *type*: `PendingMatchView` has no field for a
        pairing id or a queue ticket, so a route cannot leak one."""
        record = await _stored(matches, _record())

        view = await service.pending_match(record.light.player_id)

        assert view is not None
        assert not hasattr(view, "pairing_id")
        assert not hasattr(view, "queue_ticket_id")
        assert not hasattr(view, "reserved_until")


class TestExpiringUnansweredMatches:
    async def test_an_overdue_match_expires(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        clock: MovableClock,
    ) -> None:
        record = await _stored(matches, _record())
        clock.advance(WINDOW.total_seconds() + 1)

        expired = await service.expire_overdue(limit=10)

        assert list(expired) == [record.id]
        assert matches.matches[record.id].status is MatchRecordStatus.EXPIRED

    async def test_a_match_inside_its_window_is_left_alone(
        self, service: MatchAcceptanceService, matches: InMemoryMatchRecordRepository
    ) -> None:
        record = await _stored(matches, _record())

        assert list(await service.expire_overdue(limit=10)) == []
        assert matches.matches[record.id].status is MatchRecordStatus.PENDING_ACCEPTANCE

    async def test_an_answered_match_is_never_expired(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        clock: MovableClock,
    ) -> None:
        """The race §14 names in the other direction: both players accepted
        just before the window closed, and the sweep must not overwrite
        that."""
        record = await _stored(matches, _record())
        await service.accept(player_id=record.light.player_id, match_id=record.id)
        await service.accept(player_id=record.dark.player_id, match_id=record.id)
        clock.advance(WINDOW.total_seconds() + 1)

        assert list(await service.expire_overdue(limit=10)) == []
        assert matches.matches[record.id].status is MatchRecordStatus.ACTIVE

    async def test_expiry_publishes_the_deadline_rather_than_the_sweep_instant(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        events: RecordingPublisher,
        clock: MovableClock,
    ) -> None:
        """The fact became true when the window closed; the job's interval
        is an implementation detail of who observed it, and the outbox
        orders by `occurred_at`."""
        record = await _stored(matches, _record())
        clock.advance(WINDOW.total_seconds() + 90)

        await service.expire_overdue(limit=10)

        assert events.types() == ["game.match_acceptance_expired"]
        assert events.published[0].occurred_at == record.acceptance_deadline

    async def test_the_sweep_is_bounded(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        clock: MovableClock,
    ) -> None:
        for _ in range(5):
            await _stored(matches, _record())
        clock.advance(WINDOW.total_seconds() + 1)

        assert len(await service.expire_overdue(limit=2)) == 2

    async def test_expiring_twice_expires_nothing_the_second_time(
        self,
        service: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        clock: MovableClock,
    ) -> None:
        """Duplicate task delivery is a certainty under AD-17's
        at-least-once contract, so a second sweep must be a no-op."""
        await _stored(matches, _record())
        clock.advance(WINDOW.total_seconds() + 1)

        first = await service.expire_overdue(limit=10)
        second = await service.expire_overdue(limit=10)

        assert len(first) == 1
        assert list(second) == []
