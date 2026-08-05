"""In-memory stand-ins for `matchmaking`'s ports — A64-014.1.

What is faked here is **storage**, never the thing under test.
`QueueService` runs for real against these, so the presence rule, the
duplicate check, the transaction sequencing and the expiry arithmetic are
all genuinely exercised.

## The one deliberate simplification

`InMemoryQueueRepository.claim_due` returns due tickets in deadline order
and does not model `SKIP LOCKED`'s behaviour under two workers. That
property belongs to PostgreSQL rather than to this code, so it is asserted
where it can be — `tests/contract/test_queue_repository.py`, with two real
sessions and two real transactions — for the same reason
`tests/fakes/outbox.py` declines to reimplement the same thing.

The uniqueness rule is modelled, because it is the one storage behaviour
`QueueService`'s correctness depends on: QT-1 is enforced by a partial
unique index, and a fake that let a second live ticket through would leave
`AlreadyQueued` untested on the path that actually raises it.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from app.modules.game.public import ProductVariant
from app.modules.matchmaking.domain.exceptions import AlreadyQueued
from app.modules.matchmaking.domain.queue_pool import QueuePool
from app.modules.matchmaking.domain.queue_ticket import (
    PROVISIONAL_RATING,
    QueueSnapshot,
    QueueStatus,
    QueueTicket,
)
from app.modules.rating.domain.glicko2 import INITIAL_DEVIATION, INITIAL_VOLATILITY
from app.modules.rating.public import RatingSnapshot, SpeedClass
from app.modules.users.domain.presence import DeviceType, Presence
from app.platform.events import DomainEvent
from app.platform.outbox import OutboxEntry


class InMemoryQueueRepository:
    """The `matchmaking.queue_ticket` relation, as a dict.

    Tickets are stored as the frozen `QueueTicket` values the repository
    returns, and every transition replaces one — so a test holding a
    reference keeps seeing what it read, exactly as it would with the real
    adapter's mapped-and-detached values.
    """

    def __init__(self) -> None:
        self.tickets: dict[UUID, QueueTicket] = {}
        #: Every `claim_due` call's `claimed_by`, in order. Asserted by the
        #: tests that care whether the sweep claimed at all.
        self.claims: list[str] = []

    async def enqueue(self, ticket: QueueTicket) -> QueueTicket:
        """Refuses a second live ticket, as the partial unique index does.

        The check is on `waiting` alone and ignores `expires_at`, which is
        what the index does: a due-but-unresolved ticket still occupies the
        constraint. That asymmetry with `active_ticket` below is real
        behaviour rather than fake sloppiness — it is why
        `QueueService.join` reads through `active_ticket` first and why a
        player whose sweep is behind can still be refused.
        """
        if any(
            stored.player_id == ticket.player_id and stored.status.is_live
            for stored in self.tickets.values()
        ):
            raise AlreadyQueued("You are already in a matchmaking queue.")

        # `uq_queue_ticket__requeued_from` — A64-015.5's idempotency. Two
        # concurrent deliveries of one `match_declined` both pass the check
        # above and both insert; only one row survives. Modelled here
        # because `QueueService.requeue`'s correctness depends on it, and a
        # fake that let the second through would leave the retry path
        # untested.
        if ticket.source_ticket_id is not None and any(
            stored.source_ticket_id == ticket.source_ticket_id for stored in self.tickets.values()
        ):
            raise AlreadyQueued("That ticket has already been requeued.")

        self.tickets[ticket.id] = ticket
        return ticket

    async def cancel(self, ticket: QueueTicket) -> bool:
        stored = self.tickets.get(ticket.id)
        if stored is None or not stored.is_waiting:
            return False
        self.tickets[ticket.id] = ticket
        return True

    async def by_id(self, ticket_id: UUID) -> QueueTicket | None:
        """One ticket by id, whatever its status — A64-015.5.

        The only read here with no liveness predicate, exactly as the real
        adapter has none: the caller is a requeue, and the ticket it is
        restoring is `matched`.
        """
        return self.tickets.get(ticket_id)

    async def active_ticket(self, player_id: UUID, *, now: datetime) -> QueueTicket | None:
        for ticket in self.tickets.values():
            if ticket.player_id == player_id and ticket.status.is_live and not ticket.is_due(now):
                return ticket
        return None

    async def queue_snapshot(self, *, pool: QueuePool, now: datetime, limit: int) -> QueueSnapshot:
        live = sorted(
            (
                ticket
                for ticket in self.tickets.values()
                if ticket.pool == pool and ticket.is_waiting and not ticket.is_due(now)
            ),
            key=lambda ticket: (ticket.entered_at, ticket.id),
        )
        return QueueSnapshot(
            pool=pool,
            taken_at=now,
            # The count is over the whole predicate and the page is bounded —
            # the real adapter's two statements, modelled so a test can
            # catch a `len(tickets)` that was meant to be `waiting`.
            waiting=len(live),
            tickets=tuple(live[:limit]),
        )

    async def claim_due(
        self, *, now: datetime, limit: int, claimed_by: str
    ) -> Sequence[QueueTicket]:
        self.claims.append(claimed_by)
        return sorted(
            (
                ticket
                for ticket in self.tickets.values()
                if ticket.status.is_live and ticket.is_due(now)
            ),
            key=lambda ticket: (ticket.expires_at, ticket.id),
        )[:limit]

    async def claim_pair(
        self, ticket_ids: Sequence[UUID], *, now: datetime
    ) -> Sequence[QueueTicket]:
        """Both tickets or neither, over the same predicate the real adapter
        uses.

        The predicate is modelled and the **row lock is not** — the same
        line `claim_due` above draws. `SKIP LOCKED`'s behaviour under two
        concurrent transactions belongs to PostgreSQL, and is asserted in
        `tests/contract/test_queue_repository.py` with two real sessions.

        What is modelled is the part `PairingService`'s correctness depends
        on: a ticket another worker already reserved is not `waiting`, so
        this returns nothing and the pairing is lost — which is exactly what
        the "cannot pair one ticket twice" test drives through here.
        """
        if len(ticket_ids) != 2:
            raise ValueError("a pairing claim is exactly two tickets")

        claimed = [
            ticket
            for ticket_id in ticket_ids
            if (ticket := self.tickets.get(ticket_id)) is not None
            and ticket.is_waiting
            and not ticket.is_due(now)
        ]
        return claimed if len(claimed) == 2 else ()

    async def claim_stale_reservations(self, *, now: datetime, limit: int) -> Sequence[QueueTicket]:
        """Reservations past their own deadline, oldest first — A64-015.4.

        The predicate is modelled and the **row lock is not**, the same line
        `claim_due` and `claim_pair` above draw: `SKIP LOCKED`'s behaviour
        under two workers belongs to PostgreSQL, and is asserted in
        `tests/contract/test_queue_repository.py` with two real sessions.

        What is modelled is the part the reconciler's correctness depends
        on: a ticket that is no longer `reserved`, or whose window is still
        open, is not claimed — which is what stops the recovery job from
        breaking a pairing a live worker is in the middle of.
        """
        return sorted(
            (
                ticket
                for ticket in self.tickets.values()
                if ticket.is_reserved and ticket.reservation_lapsed(now)
            ),
            key=lambda ticket: (ticket.reserved_until or now, ticket.id),
        )[:limit]

    async def reserve(self, tickets: Sequence[QueueTicket]) -> bool:
        return self._transition(tickets, expected=QueueStatus.WAITING)

    async def release(self, tickets: Sequence[QueueTicket]) -> bool:
        return self._transition(tickets, expected=QueueStatus.RESERVED)

    async def complete(self, tickets: Sequence[QueueTicket], *, at: datetime) -> bool:
        return self._transition(tickets, expected=QueueStatus.RESERVED)

    def _transition(self, tickets: Sequence[QueueTicket], *, expected: QueueStatus) -> bool:
        """All or nothing, with the compare-and-set the real `UPDATE`
        carries.

        Checked over the whole set *before* anything is written, so a
        half-applied pair cannot be observed here either — the real adapter
        gets that from one statement, and a fake that wrote as it went would
        pass a test the database would fail.
        """
        stored = [self.tickets.get(ticket.id) for ticket in tickets]
        if any(row is None or row.status is not expected for row in stored):
            return False
        for ticket in tickets:
            self.tickets[ticket.id] = ticket
        return True

    async def expire(self, ticket_ids: Sequence[UUID], *, at: datetime) -> int:
        expired = 0
        for ticket_id in ticket_ids:
            ticket = self.tickets.get(ticket_id)
            # `status = 'waiting'` in the predicate, exactly as the real
            # `UPDATE` carries it — a ticket cancelled between the claim and
            # this write must not be re-stamped as expired.
            if ticket is None or not ticket.status.is_live:
                continue
            self.tickets[ticket_id] = ticket.expired(at)
            expired += 1
        return expired


class FixedRatingProvider:
    """Every player rates at one number, which the test chooses.

    Configurable where `ProvisionalRatingProvider` is a constant, so a test
    can assert that the ticket recorded *what the provider said* rather than
    what the domain's fallback happens to be — which is the difference
    between QT-2 being implemented and 1500 being hardcoded twice.
    """

    def __init__(self, rating: int = PROVISIONAL_RATING) -> None:
        self.rating = rating
        self.calls: list[tuple[UUID, ProductVariant, SpeedClass]] = []

    async def rating_for(
        self, player_id: UUID, *, variant: ProductVariant, speed_class: SpeedClass
    ) -> RatingSnapshot:
        """The chosen number as a Glicko-2 triple — A64-017.2.

        The port returns the whole triple since `rating` shipped, because
        the seat snapshot needs the deviation and volatility (PR-3). These
        tests are about the *ticket*, which records only the value, so the
        other two are the starting figures and are not what is asserted.
        """
        self.calls.append((player_id, variant, speed_class))
        return RatingSnapshot(
            value=float(self.rating),
            deviation=INITIAL_DEVIATION,
            volatility=INITIAL_VOLATILITY,
            games_played=0,
            is_provisional=True,
        )


class StubPresence:
    """A `PresenceProvider` a test dictates the answers of.

    Three states, because `QueueService.join` treats them differently and
    getting that wrong is the failure mode worth a fake: a *recorded*
    offline refuses, and unknown does not.

        online(player)    a live record saying `is_online=True`
        offline(player)   a live record saying `is_online=False`
        (default)         `None` — unknown, which is also what an
                          unreachable Redis produces
    """

    def __init__(self) -> None:
        self.records: dict[UUID, Presence] = {}

    def online(self, player_id: UUID, *, at: datetime) -> None:
        self.records[player_id] = Presence(
            is_online=True, last_seen=at, session_id=None, device_type=DeviceType.WEB
        )

    def offline(self, player_id: UUID, *, at: datetime) -> None:
        self.records[player_id] = Presence(
            is_online=False, last_seen=at, session_id=None, device_type=DeviceType.WEB
        )

    async def presence_for(self, player_id: UUID) -> Presence | None:
        return self.records.get(player_id)

    async def presence_for_many(self, player_ids: Sequence[UUID]) -> dict[UUID, Presence]:
        return {
            player_id: self.records[player_id]
            for player_id in player_ids
            if player_id in self.records
        }


class RecordingPublisher:
    """An `EventPublisher` that keeps what it was given.

    Not an `InMemoryOutbox`: what these tests assert is *which events a use
    case emitted and in what transaction*, and the outbox's own storage
    behaviour is covered by its own suites. Keeping the published events
    themselves rather than their payloads means a test asserts on a type and
    a field instead of on a dict of strings.
    """

    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> OutboxEntry:
        self.published.append(event)
        # A real entry, so a producer's logging and return type behave
        # exactly as they do against `OutboxEventPublisher` — the same
        # reason `NoEventPublisher` returns one rather than `None`.
        return OutboxEntry.of(event)

    def types(self) -> list[str]:
        return [type(event).event_type for event in self.published]


def waiting_ids(repository: InMemoryQueueRepository) -> set[UUID]:
    """Every ticket still in `waiting`. A helper because three tests assert
    on it and `{t.id for t in ... if t.status is QueueStatus.WAITING}` at
    each site is the kind of expression a typo hides in."""
    return {
        ticket.id for ticket in repository.tickets.values() if ticket.status is QueueStatus.WAITING
    }
