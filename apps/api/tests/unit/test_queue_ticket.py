"""`QueueTicket` — the aggregate's own rules, A64-014.1.

No database, no clock, no service. What is asserted here is exactly what
`QueueTicket` can enforce on its own: the state machine, the window, and the
two invariants a database CHECK also holds (BE-06 — the aggregate refuses
first so a caller gets a good error, the constraint refuses last so a repair
script cannot).

QT-1 is deliberately absent from this file. It spans every other row for a
player, so it is the index's rule and it is asserted where it is enforced —
`tests/unit/test_queue_service.py` for the service's read-first check, and
`tests/contract/test_queue_repository.py` for the constraint.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.exceptions import TicketNotWaiting
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType, Region
from app.modules.matchmaking.domain.queue_ticket import (
    QueueStatus,
    QueueTicket,
)
from tests.fakes.time_controls import BLITZ

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
TTL_SECONDS = 600.0

#: A64-015.4's reservation deadline. Thirty seconds, the default
#: `MATCHMAKING_RESERVATION_TTL_SECONDS`, and well inside the ticket's own
#: window — a reservation is a crash-recovery grace, not a wait.
RESERVED_UNTIL = NOW + timedelta(seconds=30)


def _ticket(*, at: datetime = NOW, ttl: float = TTL_SECONDS) -> QueueTicket:
    return QueueTicket.enter(
        player_id=generate_uuid7(),
        pool=QueuePool(
            variant=ProductVariant.RUSSIAN_8X8,
            queue_type=QueueType.RANKED,
            region=Region.EUROPE,
            time_control_id=BLITZ.id,
        ),
        time_control=BLITZ,
        rating_snapshot=1500,
        at=at,
        ttl=ttl,
    )


class TestEntering:
    def test_a_new_ticket_is_waiting_and_unresolved(self) -> None:
        ticket = _ticket()

        assert ticket.status is QueueStatus.WAITING
        assert ticket.is_waiting
        assert ticket.resolved_at is None

    def test_the_window_is_the_ttl_from_the_injected_instant(self) -> None:
        """AD-07: the deadline is arithmetic on an argument, never a clock
        read — which is what makes a ten-minute window a microsecond test."""
        ticket = _ticket(at=NOW, ttl=600)

        assert ticket.entered_at == NOW
        assert ticket.expires_at == NOW + timedelta(seconds=600)

    def test_a_ticket_records_the_rating_it_was_given(self) -> None:
        """QT-2. The snapshot comes from the provider, so a rating that
        changes while the ticket waits does not move it."""
        ticket = QueueTicket.enter(
            player_id=generate_uuid7(),
            pool=QueuePool(
                variant=ProductVariant.RUSSIAN_8X8,
                queue_type=QueueType.CASUAL,
                region=Region.GLOBAL,
                time_control_id=BLITZ.id,
            ),
            time_control=BLITZ,
            rating_snapshot=1873,
            at=NOW,
            ttl=TTL_SECONDS,
        )

        assert ticket.rating_snapshot == 1873


class TestConstruction:
    """The two invariants the database also holds (BE-06).

    Asserted on direct construction rather than through `enter`, because
    that is the path the repository takes when it rehydrates a row — which
    is what makes a corrupt row fail at the boundary rather than reach a
    response.
    """

    def test_a_window_that_closes_before_it_opens_is_refused(self) -> None:
        with pytest.raises(ValueError, match="expire before"):
            QueueTicket(
                player_id=generate_uuid7(),
                pool=QueuePool(
                    variant=ProductVariant.RUSSIAN_8X8,
                    queue_type=QueueType.RANKED,
                    region=Region.GLOBAL,
                    time_control_id=BLITZ.id,
                ),
                time_control=BLITZ,
                rating_snapshot=1500,
                entered_at=NOW,
                expires_at=NOW,
            )

    def test_a_negative_rating_is_refused(self) -> None:
        with pytest.raises(ValueError, match="rating_snapshot"):
            QueueTicket(
                player_id=generate_uuid7(),
                pool=QueuePool(
                    variant=ProductVariant.RUSSIAN_8X8,
                    queue_type=QueueType.RANKED,
                    region=Region.GLOBAL,
                    time_control_id=BLITZ.id,
                ),
                time_control=BLITZ,
                rating_snapshot=-1,
                entered_at=NOW,
                expires_at=NOW + timedelta(seconds=1),
            )

    def test_a_terminal_ticket_without_its_instant_is_refused(self) -> None:
        """`ck_queue_ticket__resolved_iff_terminal`, in the domain. A ticket
        that claims an outcome with no `resolved_at` is a row nothing can
        explain."""
        with pytest.raises(ValueError, match="resolved_at"):
            QueueTicket(
                player_id=generate_uuid7(),
                pool=QueuePool(
                    variant=ProductVariant.RUSSIAN_8X8,
                    queue_type=QueueType.RANKED,
                    region=Region.GLOBAL,
                    time_control_id=BLITZ.id,
                ),
                time_control=BLITZ,
                rating_snapshot=1500,
                entered_at=NOW,
                expires_at=NOW + timedelta(seconds=1),
                status=QueueStatus.CANCELLED,
            )

    def test_a_waiting_ticket_with_an_instant_is_refused(self) -> None:
        """The other direction of the same pairing — the one a naive check
        of "is resolved_at set when terminal" would miss."""
        with pytest.raises(ValueError, match="resolved_at"):
            QueueTicket(
                player_id=generate_uuid7(),
                pool=QueuePool(
                    variant=ProductVariant.RUSSIAN_8X8,
                    queue_type=QueueType.RANKED,
                    region=Region.GLOBAL,
                    time_control_id=BLITZ.id,
                ),
                time_control=BLITZ,
                rating_snapshot=1500,
                entered_at=NOW,
                expires_at=NOW + timedelta(seconds=1),
                status=QueueStatus.WAITING,
                resolved_at=NOW,
            )


class TestDueness:
    def test_a_fresh_ticket_is_not_due(self) -> None:
        assert not _ticket().is_due(NOW)

    def test_a_ticket_is_due_at_exactly_its_deadline(self) -> None:
        """Non-strict on the boundary, deliberately: the sweeper and
        `active_ticket` both read this, and "not yet, come back in a
        microsecond" is not a distinction either can act on."""
        ticket = _ticket()

        assert ticket.is_due(ticket.expires_at)

    def test_a_ticket_is_due_after_its_deadline(self) -> None:
        ticket = _ticket()

        assert ticket.is_due(ticket.expires_at + timedelta(seconds=1))


class TestTransitions:
    def test_cancelling_produces_a_resolved_ticket(self) -> None:
        ticket = _ticket()

        cancelled = ticket.cancelled(NOW + timedelta(seconds=30))

        assert cancelled.status is QueueStatus.CANCELLED
        assert cancelled.resolved_at == NOW + timedelta(seconds=30)

    def test_expiring_produces_a_resolved_ticket(self) -> None:
        ticket = _ticket()

        expired = ticket.expired(ticket.expires_at)

        assert expired.status is QueueStatus.EXPIRED
        assert expired.resolved_at == ticket.expires_at

    def test_matching_produces_a_resolved_ticket(self) -> None:
        """A64-015.3 narrowed the guard: `matched` is now reachable only
        from `reserved`. §8 forbids marking a ticket matched before `game`
        has accepted the request, and requiring the reservation is how that
        is enforced rather than remembered."""
        ticket = _ticket().reserved(until=RESERVED_UNTIL)

        matched = ticket.matched(NOW + timedelta(seconds=5))

        assert matched.status is QueueStatus.MATCHED
        assert matched.resolved_at == NOW + timedelta(seconds=5)

    def test_a_waiting_ticket_cannot_be_matched(self) -> None:
        """The guard that makes "no match before `game` accepts" a property
        of the aggregate rather than a convention in the service."""
        with pytest.raises(TicketNotWaiting):
            _ticket().matched(NOW + timedelta(seconds=5))

    def test_a_transition_leaves_the_original_untouched(self) -> None:
        """Frozen, and it matters: the repository's compare-and-set needs
        the before *and* the after, so a mutating transition would leave the
        caller holding only one of them."""
        ticket = _ticket()

        ticket.cancelled(NOW)

        assert ticket.status is QueueStatus.WAITING

    def test_a_transition_preserves_identity_and_the_pool(self) -> None:
        ticket = _ticket()

        cancelled = ticket.cancelled(NOW)

        assert cancelled.id == ticket.id
        assert cancelled.player_id == ticket.player_id
        assert cancelled.queue_type is ticket.queue_type
        assert cancelled.region is ticket.region
        assert cancelled.rating_snapshot == ticket.rating_snapshot
        assert cancelled.entered_at == ticket.entered_at
        assert cancelled.expires_at == ticket.expires_at

    @pytest.mark.parametrize("second", ["cancelled", "expired", "matched"])
    def test_a_resolved_ticket_cannot_transition_again(self, second: str) -> None:
        """The guard that makes four values a state *machine*. A cancelled
        ticket must not later expire, or the row would report a departure
        the player did not make."""
        resolved = _ticket().cancelled(NOW)

        with pytest.raises(TicketNotWaiting):
            getattr(resolved, second)(NOW + timedelta(seconds=1))
