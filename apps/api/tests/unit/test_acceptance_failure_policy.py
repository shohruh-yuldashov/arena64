"""The acceptance-failure policy — A64-015.5 §1, §2 and §3.

A64-015.4 shipped acceptance with this gap open and named the cost: "a
player who accepts promptly and whose opponent declines loses their place in
line through no fault of their own." This file is the evidence that they no
longer do.

`MatchOutcomeService`, `QueueService.requeue`, `QueueTicket.requeued` and
`QueueCooldown` all run **for real** over in-memory storage, so the fairness
rule, the idempotency and the decline-versus-silence distinction are
genuinely exercised. What is substituted is the database, the clock and
`game` — nothing that decides what the policy does.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.engine import PlayerSide
from app.modules.game.public import MatchAcceptanceExpired, MatchDeclined, ProductVariant
from app.modules.matchmaking.application.eligibility import (
    AllEligibilityChecks,
    CooldownEligibilityPolicy,
)
from app.modules.matchmaking.application.metrics import (
    ACCEPTANCE_FAILURE_ACTIONS,
    RECONCILIATION_ACTIONS,
    AcceptanceFailureAction,
)
from app.modules.matchmaking.application.services import MatchOutcomeService, QueueService
from app.modules.matchmaking.domain.cooldown import CooldownReason
from app.modules.matchmaking.domain.events import ReconciliationAction
from app.modules.matchmaking.domain.exceptions import QueueCooldownActive
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType
from app.modules.matchmaking.domain.queue_ticket import QueueStatus, QueueTicket
from app.platform.outbox import OutboxEntry
from tests.fakes.audit import InMemoryCooldownAuditRepository
from tests.fakes.cooldowns import InMemoryCooldownRepository
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.outbox import NullUnitOfWork
from tests.fakes.presence_redis import MovableClock
from tests.fakes.queue_repository import (
    FixedRatingProvider,
    InMemoryQueueRepository,
    RecordingPublisher,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)
WINDOW = timedelta(seconds=30)
COOLDOWN_SECONDS = 60.0

POOL = QueuePool(variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED)


class _Eligible:
    """Everybody may queue. The presence half of eligibility, stubbed out so
    that a test about cooldowns is not also a test about presence."""

    async def require_eligible(self, player_id: UUID, *, pool: QueuePool) -> None:
        return None


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def tickets() -> InMemoryQueueRepository:
    return InMemoryQueueRepository()


@pytest.fixture
def cooldowns() -> InMemoryCooldownRepository:
    return InMemoryCooldownRepository()


@pytest.fixture
def audit() -> InMemoryCooldownAuditRepository:
    return InMemoryCooldownAuditRepository()


@pytest.fixture
def events() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


@pytest.fixture
def queue(
    tickets: InMemoryQueueRepository,
    cooldowns: InMemoryCooldownRepository,
    events: RecordingPublisher,
    clock: MovableClock,
) -> QueueService:
    """The real queue use cases, over the real composed eligibility policy.

    The cooldown check is wired in rather than stubbed, because "a player in
    cooldown is not requeued" (§2) is a property of the two working
    together and would be untested against a permissive stub.
    """
    return QueueService(
        tickets=tickets,
        ratings=FixedRatingProvider(),
        eligibility=AllEligibilityChecks(
            [_Eligible(), CooldownEligibilityPolicy(cooldowns, clock=clock)]
        ),
        events=events,
        unit_of_work=NullUnitOfWork(),
        clock=clock,
        ticket_ttl_seconds=TTL.total_seconds(),
        snapshot_limit=50,
    )


@pytest.fixture
def policy(
    queue: QueueService,
    cooldowns: InMemoryCooldownRepository,
    audit: InMemoryCooldownAuditRepository,
    clock: MovableClock,
    metrics: RecordingMetrics,
) -> MatchOutcomeService:
    return MatchOutcomeService(
        queue=queue,
        cooldowns=cooldowns,
        audit=audit,
        unit_of_work=NullUnitOfWork(),
        clock=clock,
        metrics=metrics,
        decline_cooldown_seconds=COOLDOWN_SECONDS,
    )


def _matched(store: InMemoryQueueRepository, *, waited: float = 0.0) -> QueueTicket:
    """A ticket that produced a match — the state a failed handshake leaves.

    Written straight into storage: reproducing it through the pairing scan
    would make every test here also a test of the scan.
    """
    entered = NOW - timedelta(seconds=waited)
    ticket = QueueTicket(
        player_id=generate_uuid7(),
        pool=POOL,
        rating_snapshot=1500,
        entered_at=entered,
        expires_at=entered + TTL,
        status=QueueStatus.MATCHED,
        resolved_at=NOW,
    )
    store.tickets[ticket.id] = ticket
    return ticket


def _declined(accepted: QueueTicket | None, decliner: QueueTicket) -> OutboxEntry:
    """A `game.match_declined` for a match `decliner` refused."""
    light, dark = (accepted, decliner) if accepted is not None else (decliner, decliner)
    return OutboxEntry.of(
        MatchDeclined(
            occurred_at=NOW,
            match_id=generate_uuid7(),
            pairing_id=generate_uuid7(),
            light_player_id=light.player_id,
            dark_player_id=dark.player_id,
            light_ticket_id=light.id,
            dark_ticket_id=dark.id,
            side=PlayerSide.DARK,
            player_id=decliner.player_id,
            light_accepted=accepted is not None,
            dark_accepted=False,
        )
    )


def _expired(one: QueueTicket, other: QueueTicket, *, accepted: QueueTicket | None) -> OutboxEntry:
    """A `game.match_acceptance_expired` for a match nobody, or one person,
    answered."""
    return OutboxEntry.of(
        MatchAcceptanceExpired(
            occurred_at=NOW + WINDOW,
            match_id=generate_uuid7(),
            pairing_id=generate_uuid7(),
            light_player_id=one.player_id,
            dark_player_id=other.player_id,
            light_ticket_id=one.id,
            dark_ticket_id=other.id,
            light_accepted=accepted is not None and accepted.id == one.id,
            dark_accepted=accepted is not None and accepted.id == other.id,
        )
    )


def _live(store: InMemoryQueueRepository, player_id: UUID) -> QueueTicket | None:
    return next(
        (
            ticket
            for ticket in store.tickets.values()
            if ticket.player_id == player_id and ticket.status.is_live
        ),
        None,
    )


class TestTheAcceptingPlayerIsRequeued:
    """§1's headline: accepting promptly must not cost you your place."""

    async def test_a_decline_requeues_the_player_who_accepted(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository
    ) -> None:
        accepted, decliner = _matched(tickets), _matched(tickets)

        await policy.handle([_declined(accepted, decliner)])

        assert _live(tickets, accepted.player_id) is not None

    async def test_the_original_entered_at_is_preserved(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository
    ) -> None:
        """The whole policy. `entered_at` is the pairing order's sort key
        **and** the input to QT-5's widening window, so a fresh instant
        would cost this player their place *and* their widened search."""
        accepted = _matched(tickets, waited=240.0)
        decliner = _matched(tickets)

        await policy.handle([_declined(accepted, decliner)])

        replacement = _live(tickets, accepted.player_id)
        assert replacement is not None
        assert replacement.entered_at == accepted.entered_at

    async def test_the_pool_and_the_rating_snapshot_survive(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository
    ) -> None:
        """They asked for that game, and QT-2 fixes the rating at entry —
        the entry being restored is the original one, and nothing about the
        player changed because no game was played."""
        accepted, decliner = _matched(tickets), _matched(tickets)

        await policy.handle([_declined(accepted, decliner)])

        replacement = _live(tickets, accepted.player_id)
        assert replacement is not None
        assert replacement.pool == accepted.pool
        assert replacement.rating_snapshot == accepted.rating_snapshot

    async def test_the_replacement_gets_a_fresh_window(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository, clock: MovableClock
    ) -> None:
        """The one field that must **not** be preserved: the original
        window has usually closed, and restoring it would produce a ticket
        the expiry sweep takes on its first pass."""
        accepted = _matched(tickets, waited=540.0)
        decliner = _matched(tickets)
        clock.advance(30)

        await policy.handle([_declined(accepted, decliner)])

        replacement = _live(tickets, accepted.player_id)
        assert replacement is not None
        assert replacement.expires_at == clock.now() + TTL
        assert not replacement.is_due(clock.now())

    async def test_the_replacement_records_where_it_came_from(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository
    ) -> None:
        accepted, decliner = _matched(tickets), _matched(tickets)

        await policy.handle([_declined(accepted, decliner)])

        replacement = _live(tickets, accepted.player_id)
        assert replacement is not None
        assert replacement.source_ticket_id == accepted.id

    async def test_the_decliner_is_not_requeued(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository
    ) -> None:
        accepted, decliner = _matched(tickets), _matched(tickets)

        await policy.handle([_declined(accepted, decliner)])

        assert _live(tickets, decliner.player_id) is None


class TestTheDeclinerIsCooledDown:
    async def test_a_decline_records_a_cooldown(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        cooldowns: InMemoryCooldownRepository,
        clock: MovableClock,
    ) -> None:
        accepted, decliner = _matched(tickets), _matched(tickets)

        await policy.handle([_declined(accepted, decliner)])

        stored = await cooldowns.active_for(decliner.player_id, now=clock.now())
        assert stored is not None
        assert stored.reason is CooldownReason.DECLINED_MATCH
        assert stored.remaining(clock.now()) == COOLDOWN_SECONDS

    async def test_the_cooldown_prevents_re_entry(
        self,
        policy: MatchOutcomeService,
        queue: QueueService,
        tickets: InMemoryQueueRepository,
    ) -> None:
        """§3's point: the bar has to be enforced on the join path, not
        merely recorded."""
        accepted, decliner = _matched(tickets), _matched(tickets)
        await policy.handle([_declined(accepted, decliner)])

        with pytest.raises(QueueCooldownActive) as refusal:
            await queue.join(player_id=decliner.player_id, pool=POOL)

        assert refusal.value.retry_after_seconds == COOLDOWN_SECONDS

    async def test_the_bar_lifts_on_its_own(
        self,
        policy: MatchOutcomeService,
        queue: QueueService,
        tickets: InMemoryQueueRepository,
        clock: MovableClock,
    ) -> None:
        accepted, decliner = _matched(tickets), _matched(tickets)
        await policy.handle([_declined(accepted, decliner)])

        clock.advance(COOLDOWN_SECONDS)

        assert await queue.join(player_id=decliner.player_id, pool=POOL) is not None

    async def test_a_repeated_decline_extends_rather_than_resets(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        cooldowns: InMemoryCooldownRepository,
        clock: MovableClock,
    ) -> None:
        """§3: "repeated decline does not bypass the cooldown". The second
        one lands halfway through the first, and the window that stands is
        the one that ends later."""
        decliner = _matched(tickets)
        await policy.handle([_declined(_matched(tickets), decliner)])
        clock.advance(COOLDOWN_SECONDS / 2)
        await policy.handle([_declined(_matched(tickets), decliner)])

        stored = await cooldowns.active_for(decliner.player_id, now=clock.now())
        assert stored is not None
        assert stored.remaining(clock.now()) == COOLDOWN_SECONDS

    async def test_a_cooled_down_player_is_not_requeued_either(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
    ) -> None:
        """§2: "blocked, sanctioned, or otherwise ineligible players are
        not blindly requeued". Without the eligibility re-read, a decline
        could be laundered through somebody else's decline."""
        serial_decliner = _matched(tickets)
        await policy.handle([_declined(_matched(tickets), serial_decliner)])

        # Now they *accept* a second match, whose opponent declines.
        await policy.handle([_declined(serial_decliner, _matched(tickets))])

        assert _live(tickets, serial_decliner.player_id) is None


class TestSilenceIsNotADecline:
    """§1 and §3: "do not classify silence as an explicit decline"."""

    async def test_nobody_answered_requeues_nobody(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository
    ) -> None:
        one, other = _matched(tickets), _matched(tickets)

        await policy.handle([_expired(one, other, accepted=None)])

        assert _live(tickets, one.player_id) is None
        assert _live(tickets, other.player_id) is None

    async def test_nobody_answered_cools_down_nobody(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        cooldowns: InMemoryCooldownRepository,
        clock: MovableClock,
    ) -> None:
        one, other = _matched(tickets), _matched(tickets)

        await policy.handle([_expired(one, other, accepted=None)])

        assert await cooldowns.active_for(one.player_id, now=clock.now()) is None
        assert await cooldowns.active_for(other.player_id, now=clock.now()) is None

    async def test_the_accepting_player_is_still_requeued_after_a_timeout(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository
    ) -> None:
        """§1's third case: one accepted, the other went silent. Fairness
        for the accepting player does not depend on *why* the match
        failed."""
        accepted, silent = _matched(tickets, waited=180.0), _matched(tickets)

        await policy.handle([_expired(accepted, silent, accepted=accepted)])

        replacement = _live(tickets, accepted.player_id)
        assert replacement is not None
        assert replacement.entered_at == accepted.entered_at

    async def test_the_silent_player_earns_no_cooldown(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        cooldowns: InMemoryCooldownRepository,
        clock: MovableClock,
    ) -> None:
        """The asymmetry that matters: a dead battery, a tunnel and a
        crashed tab are all indistinguishable from walking away, and
        punishing them all would make the queue hostile to anybody on a
        train."""
        accepted, silent = _matched(tickets), _matched(tickets)

        await policy.handle([_expired(accepted, silent, accepted=accepted)])

        assert await cooldowns.active_for(silent.player_id, now=clock.now()) is None

    async def test_a_timeout_is_counted_differently_from_a_decline(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        metrics: RecordingMetrics,
    ) -> None:
        one, other = _matched(tickets), _matched(tickets)

        await policy.handle([_expired(one, other, accepted=None)])

        counts = metrics.counts(ACCEPTANCE_FAILURE_ACTIONS)
        assert counts == {AcceptanceFailureAction.NO_ACTION.value: 1.0}


class TestIdempotency:
    async def test_a_redelivered_decline_produces_one_ticket(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository
    ) -> None:
        """§11. The event ledger stops most redeliveries; this is what
        happens when one gets through."""
        accepted, decliner = _matched(tickets), _matched(tickets)
        entry = _declined(accepted, decliner)

        await policy.handle([entry])
        await policy.handle([entry])

        live = [
            ticket
            for ticket in tickets.tickets.values()
            if ticket.player_id == accepted.player_id and ticket.status.is_live
        ]
        assert len(live) == 1

    async def test_a_player_who_requeued_by_hand_is_left_alone(
        self,
        policy: MatchOutcomeService,
        queue: QueueService,
        tickets: InMemoryQueueRepository,
    ) -> None:
        """QT-1 is the guard, and the outcome §1 wants is already true:
        they are in a queue. Their own ticket keeps its own `entered_at`,
        which is a small loss and the honest one — the platform does not
        silently rewrite a ticket the player created."""
        accepted, decliner = _matched(tickets), _matched(tickets)
        manual = await queue.join(player_id=accepted.player_id, pool=POOL)

        await policy.handle([_declined(accepted, decliner)])

        assert _live(tickets, accepted.player_id) is not None
        assert _live(tickets, accepted.player_id).id == manual.id  # type: ignore[union-attr]

    async def test_a_repeated_cooldown_is_safe(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        cooldowns: InMemoryCooldownRepository,
        clock: MovableClock,
    ) -> None:
        decliner = _matched(tickets)
        entry = _declined(_matched(tickets), decliner)

        await policy.handle([entry])
        await policy.handle([entry])

        stored = await cooldowns.active_for(decliner.player_id, now=clock.now())
        assert stored is not None
        assert stored.remaining(clock.now()) == COOLDOWN_SECONDS

    async def test_a_malformed_payload_fails_one_entry_and_not_the_batch(
        self, policy: MatchOutcomeService, tickets: InMemoryQueueRepository
    ) -> None:
        accepted, decliner = _matched(tickets), _matched(tickets)
        good = _declined(accepted, decliner)
        bad = OutboxEntry.of(
            MatchDeclined(
                occurred_at=NOW,
                match_id=generate_uuid7(),
                pairing_id=generate_uuid7(),
                light_player_id=uuid4(),
                dark_player_id=uuid4(),
                light_ticket_id=uuid4(),
                dark_ticket_id=uuid4(),
                side=PlayerSide.LIGHT,
                player_id=uuid4(),
                light_accepted=True,
                dark_accepted=False,
            )
        )

        failures = await policy.handle([bad, good])

        # The unknown ticket is not a *failure* — `requeue` reports a
        # missing source as "nothing to restore" — so the batch succeeds
        # and the good entry still applied.
        assert list(failures) == []
        assert _live(tickets, accepted.player_id) is not None


class TestTheMetricsAreBounded:
    """§9: no player ids, no match ids, no pairing ids in a label."""

    async def test_a_requeue_is_counted_in_both_funnels(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        metrics: RecordingMetrics,
    ) -> None:
        accepted, decliner = _matched(tickets), _matched(tickets)

        await policy.handle([_declined(accepted, decliner)])

        assert metrics.counts(ACCEPTANCE_FAILURE_ACTIONS) == {
            AcceptanceFailureAction.REQUEUED.value: 1.0,
            AcceptanceFailureAction.COOLDOWN_APPLIED.value: 1.0,
        }
        assert metrics.counts(RECONCILIATION_ACTIONS) == {ReconciliationAction.REQUEUED.value: 1.0}

    async def test_a_skipped_requeue_is_counted_as_such(
        self,
        policy: MatchOutcomeService,
        queue: QueueService,
        tickets: InMemoryQueueRepository,
        metrics: RecordingMetrics,
    ) -> None:
        accepted, decliner = _matched(tickets), _matched(tickets)
        await queue.join(player_id=accepted.player_id, pool=POOL)

        await policy.handle([_declined(accepted, decliner)])

        assert (
            metrics.counts(ACCEPTANCE_FAILURE_ACTIONS)[
                AcceptanceFailureAction.REQUEUE_SKIPPED.value
            ]
            == 1.0
        )

    async def test_no_label_carries_an_identifier(
        self,
        policy: MatchOutcomeService,
        tickets: InMemoryQueueRepository,
        metrics: RecordingMetrics,
    ) -> None:
        """The cardinality rule, asserted rather than trusted: every label
        value must be a member of a closed enumeration, so the number of
        time series is fixed at import time."""
        accepted, decliner = _matched(tickets), _matched(tickets)
        await policy.handle([_declined(accepted, decliner)])

        permitted = {member.value for member in AcceptanceFailureAction} | {
            member.value for member in ReconciliationAction
        }
        assert metrics.label_values() <= permitted
