"""Realtime pending-match delivery — A64-015.5 §4, §6 and §11.

`PendingMatchNotifier` runs **for real** over in-memory storage and a
recording sink, so the re-read, the deadline check, the block re-check and
the batching are all genuinely exercised. What is substituted is the
database, the clock and the transport.

The property this file is mostly about is §6: **nothing is trusted from the
payload except identity.** Every test below that asserts a non-delivery is
asserting that the consumer asked a question at delivery time rather than
believing what the event said when it was written.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION, PlayerSide
from app.modules.game.application.services import MatchAcceptanceService
from app.modules.game.domain.match_record import MatchRecord, MatchSeat
from app.modules.game.public import MatchCreated, ProductVariant
from app.modules.matchmaking.application.metrics import (
    PENDING_MATCH_DELIVERIES,
    DeliveryOutcome,
)
from app.modules.matchmaking.application.services import PendingMatchNotifier
from app.modules.matchmaking.domain.pending_match import PendingMatchOffer
from app.platform.outbox import OutboxEntry
from tests.fakes.clock_deadlines import RecordingClockDeadlines
from tests.fakes.matches import InMemoryMatchRecordRepository
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.pairing import StubExclusions
from tests.fakes.pending_matches import RecordingPendingMatchSink, StubPublicProfiles
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import RecordingPublisher

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
WINDOW = timedelta(seconds=30)


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def matches() -> InMemoryMatchRecordRepository:
    return InMemoryMatchRecordRepository()


@pytest.fixture
def acceptance(
    matches: InMemoryMatchRecordRepository, clock: MovableClock
) -> MatchAcceptanceService:
    """The **real** acceptance service, so "still pending" is answered by
    the same read a route would use rather than by a stub that agrees."""
    return MatchAcceptanceService(
        deadlines=RecordingClockDeadlines(),
        matches=matches,
        events=RecordingPublisher(),
        unit_of_work=NullUnitOfWork(),
        clock=clock,
        metrics=RecordingMetrics(),
    )


@pytest.fixture
def exclusions() -> StubExclusions:
    return StubExclusions()


@pytest.fixture
def players() -> StubPublicProfiles:
    return StubPublicProfiles()


@pytest.fixture
def sink() -> RecordingPendingMatchSink:
    return RecordingPendingMatchSink()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


@pytest.fixture
def notifier(
    acceptance: MatchAcceptanceService,
    exclusions: StubExclusions,
    players: StubPublicProfiles,
    sink: RecordingPendingMatchSink,
    clock: MovableClock,
    metrics: RecordingMetrics,
) -> PendingMatchNotifier:
    return PendingMatchNotifier(
        acceptance=acceptance,
        exclusions=exclusions,
        players=players,
        sink=sink,
        clock=clock,
        metrics=metrics,
    )


async def _created(
    matches: InMemoryMatchRecordRepository,
    players: StubPublicProfiles,
    *,
    at: datetime = NOW,
) -> tuple[MatchRecord, OutboxEntry]:
    """A pending match and the `game.match_created` it produced."""
    record = MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(player_id=generate_uuid7(), queue_ticket_id=generate_uuid7()),
        dark=MatchSeat(player_id=generate_uuid7(), queue_ticket_id=generate_uuid7()),
        created_at=at,
        acceptance_deadline=at + WINDOW,
    )
    stored, _ = await matches.create(record)
    players.register(stored.light.player_id, "alice")
    players.register(stored.dark.player_id, "bob")

    entry = OutboxEntry.of(
        MatchCreated(
            occurred_at=stored.created_at,
            match_id=stored.id,
            pairing_id=stored.pairing_id,
            light_player_id=stored.light.player_id,
            dark_player_id=stored.dark.player_id,
            light_ticket_id=stored.light.queue_ticket_id,
            dark_ticket_id=stored.dark.queue_ticket_id,
            variant=stored.variant,
            rated=stored.rated,
            acceptance_deadline=stored.acceptance_deadline,
        )
    )
    return stored, entry


def _for(sink: RecordingPendingMatchSink, player_id: UUID) -> list[PendingMatchOffer]:
    return [offer for offer in sink.delivered if offer.recipient_id == player_id]


class TestACreatedMatchIsPushedToBothPlayers:
    async def test_both_participants_receive_an_offer(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        record, entry = await _created(matches, players)

        await notifier.handle([entry])

        assert len(sink.delivered) == 2
        assert {offer.recipient_id for offer in sink.delivered} == set(record.player_ids())

    async def test_each_offer_is_addressed_from_its_recipient_s_seat(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """The whole reason `PendingMatchOffer` is a different type from
        `PendingMatchView`: a delivery is addressed, and a client must not
        have to work out which half of the payload is theirs."""
        record, entry = await _created(matches, players)

        await notifier.handle([entry])

        light = _for(sink, record.light.player_id)[0]
        dark = _for(sink, record.dark.player_id)[0]
        # `opponent` is optional because a withdrawn account is omitted
        # rather than previewed; here both players exist, so its presence is
        # part of what this asserts.
        assert light.opponent is not None
        assert dark.opponent is not None
        assert light.opponent.player_id == record.dark.player_id
        assert dark.opponent.player_id == record.light.player_id

    async def test_the_offer_carries_what_a_client_needs_to_answer(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """§4's list: identifier, side, variant, rated, deadline, opponent
        preview. Time control is absent for the reason §10.8 of the spec
        records — `reference.time_control` does not exist in code."""
        record, entry = await _created(matches, players)

        await notifier.handle([entry])

        light = _for(sink, record.light.player_id)[0]

        assert light.match_id == record.id
        assert light.variant is ProductVariant.RUSSIAN_8X8
        assert light.rated is True
        assert light.acceptance_deadline == record.acceptance_deadline
        assert light.your_side is PlayerSide.LIGHT
        assert light.opponent is not None

    async def test_the_offer_says_how_long_is_left(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
        clock: MovableClock,
    ) -> None:
        _, entry = await _created(matches, players)
        clock.advance(10)

        await notifier.handle([entry])

        assert sink.delivered[0].remaining_seconds(clock.now()) == 20.0

    async def test_the_profile_read_is_batched_across_the_tick(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """A relay page of matches must cost one profile query, not two per
        match — the N+1 a consumer running on every tick is the worst place
        to contain."""
        entries = [(await _created(matches, players))[1] for _ in range(3)]

        await notifier.handle(entries)

        assert len(sink.delivered) == 6
        assert len(players.calls) == 1


class TestStaleEventsAreNotDelivered:
    """§6: enqueue-time state is not trusted."""

    async def test_an_answered_match_is_not_delivered(
        self,
        notifier: PendingMatchNotifier,
        acceptance: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        record, entry = await _created(matches, players)
        await acceptance.decline(player_id=record.light.player_id, match_id=record.id)

        await notifier.handle([entry])

        assert sink.delivered == []

    async def test_a_match_both_players_already_accepted_is_not_delivered(
        self,
        notifier: PendingMatchNotifier,
        acceptance: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """A64-020.5A. `pending_match` now reports a match that has already
        **started**, which is what a lobby needs and is exactly what must
        not be pushed as an offer.

        The window is real: both players can agree before the relay reaches
        the `match_created` entry that announced the offer. Delivering it
        then would open an acceptance dialog over a game already in
        progress — worse than delivering nothing, because the game is the
        thing the player is now trying to look at.
        """
        record, entry = await _created(matches, players)
        for player_id in record.player_ids():
            await acceptance.accept(player_id=player_id, match_id=record.id)

        await notifier.handle([entry])

        assert sink.delivered == []

    async def test_a_match_past_its_deadline_is_not_delivered(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
        clock: MovableClock,
        metrics: RecordingMetrics,
    ) -> None:
        """Still `pending_acceptance` — the reconciler has not reached it —
        and out of time. Pushing it would start a client's countdown below
        zero."""
        _, entry = await _created(matches, players)
        clock.advance(WINDOW.total_seconds() + 1)

        await notifier.handle([entry])

        assert sink.delivered == []
        assert metrics.counts(PENDING_MATCH_DELIVERIES) == {
            DeliveryOutcome.DEADLINE_PASSED.value: 2.0
        }

    async def test_only_the_player_who_already_answered_is_skipped(
        self,
        notifier: PendingMatchNotifier,
        acceptance: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """One acceptance leaves the match pending, so both are still owed
        the offer — the accepting one so their client can render "waiting
        for your opponent"."""
        record, entry = await _created(matches, players)
        await acceptance.accept(player_id=record.light.player_id, match_id=record.id)

        await notifier.handle([entry])

        assert len(sink.delivered) == 2
        light = _for(sink, record.light.player_id)[0]
        assert light.you_accepted is True
        assert light.opponent_accepted is False

    async def test_a_stale_event_is_counted(
        self,
        notifier: PendingMatchNotifier,
        acceptance: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        metrics: RecordingMetrics,
    ) -> None:
        record, entry = await _created(matches, players)
        await acceptance.decline(player_id=record.light.player_id, match_id=record.id)

        await notifier.handle([entry])

        assert metrics.counts(PENDING_MATCH_DELIVERIES) == {DeliveryOutcome.STALE.value: 2.0}


class TestPrivacyAtDeliveryTime:
    async def test_a_block_withholds_the_name_and_not_the_match(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        exclusions: StubExclusions,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """Withholding the offer would leave a player holding a match they
        cannot see, which the deadline would then expire against them.
        Withholding the name costs them a face on a card."""
        record, entry = await _created(matches, players)
        exclusions.block(record.light.player_id, record.dark.player_id)

        await notifier.handle([entry])

        assert len(sink.delivered) == 2
        assert all(offer.opponent is None for offer in sink.delivered)

    async def test_a_deactivated_opponent_produces_no_preview(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """`find_public_profiles` omits withdrawn accounts, which is the
        same answer every other surface on this platform gives."""
        record, entry = await _created(matches, players)
        players.deactivate(record.dark.player_id)

        await notifier.handle([entry])

        assert _for(sink, record.light.player_id)[0].opponent is None
        assert _for(sink, record.dark.player_id)[0].opponent is not None

    async def test_an_unreadable_block_graph_still_delivers(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        exclusions: StubExclusions,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """The direction is chosen: a player who never learns they have a
        match loses it to the deadline, and BL-2 already made a blocked
        pairing impossible at the point it mattered most."""
        _, entry = await _created(matches, players)
        exclusions.fails = True

        await notifier.handle([entry])

        assert len(sink.delivered) == 2


class TestIdempotencyAndFailure:
    async def test_a_duplicate_event_delivers_a_consistent_offer(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """§11. An offer is a **statement**, not a command: re-delivering
        one is either still true or no longer built. Nothing accumulates."""
        record, entry = await _created(matches, players)

        await notifier.handle([entry])
        await notifier.handle([entry])

        assert len(sink.delivered) == 4
        assert {offer.match_id for offer in sink.delivered} == {record.id}
        assert {offer.status for offer in sink.delivered} == {record.status}

    async def test_a_duplicate_after_the_match_settles_delivers_nothing(
        self,
        notifier: PendingMatchNotifier,
        acceptance: MatchAcceptanceService,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        record, entry = await _created(matches, players)
        await notifier.handle([entry])
        await acceptance.decline(player_id=record.dark.player_id, match_id=record.id)

        await notifier.handle([entry])

        assert len(sink.delivered) == 2

    async def test_a_sink_failure_is_reported_for_retry(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        """A delivery that failed is one the platform should retry, so it
        propagates to the relay rather than being marked published."""
        _, entry = await _created(matches, players)
        sink.fails = True

        failures = await notifier.handle([entry])

        assert [failure.entry_id for failure in failures] == [entry.id]

    async def test_one_bad_entry_does_not_fail_the_batch(
        self,
        notifier: PendingMatchNotifier,
        matches: InMemoryMatchRecordRepository,
        players: StubPublicProfiles,
        sink: RecordingPendingMatchSink,
    ) -> None:
        _, good = await _created(matches, players)
        bad = OutboxEntry.of(
            MatchCreated(
                occurred_at=NOW,
                match_id=generate_uuid7(),
                pairing_id=generate_uuid7(),
                light_player_id=generate_uuid7(),
                dark_player_id=generate_uuid7(),
                light_ticket_id=generate_uuid7(),
                dark_ticket_id=generate_uuid7(),
                variant=ProductVariant.RUSSIAN_8X8,
                rated=True,
                acceptance_deadline=NOW + WINDOW,
            )
        )

        failures = await notifier.handle([bad, good])

        # The unknown match is *stale*, not malformed — no such pending
        # match exists — so it is skipped rather than failed.
        assert list(failures) == []
        assert len(sink.delivered) == 2


class TestTheConsumerContract:
    def test_it_subscribes_to_match_created_only(self, notifier: PendingMatchNotifier) -> None:
        """Subscribing to acceptances and declines as well would make this a
        general match-state feed — the live-game protocol A64-015.5
        excludes by name."""
        assert notifier.handles(MatchCreated.event_type)
        assert not notifier.handles("game.match_activated")
        assert not notifier.handles("game.match_declined")

    def test_it_has_its_own_ledger_partition(self, notifier: PendingMatchNotifier) -> None:
        """So a redelivery the acceptance-failure policy has handled can
        still reach this one, and neither marks the other's work done."""
        assert notifier.consumer == "matchmaking_pending_match"
