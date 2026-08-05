"""The registration use cases — SPEC-TOURNAMENT §4, A64-019.2 §8.

Six operations, one transaction each, and every durable fact published
through the outbox in the same transaction that wrote it (AD-16).

## Capacity is held by a lock, not by a check

`register` takes `FOR UPDATE` on the tournament row **before** counting.
Two players entering the last slot at the same instant serialise on that
row, so the second sees the first's insert and is refused. Counting first
and inserting after — outside a lock — is exactly the race §6 forbids, and
it is invisible in any test that registers one player at a time.

The unique key is a different guarantee for a different question: it stops
one player entering twice, and says nothing about how many players there
are. Both are needed.

## Why the participant check is a published read

§3 — `users.public`. A tournament must not invent its own idea of who
exists, and importing `users`' internals would be the coupling the import
contract refuses. The check happens **before** the lock, because it is a
read about a player rather than about the tournament and holding a row lock
across it would serialise registrations behind an unrelated query.
"""

import logging
from datetime import datetime
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.game.public import ProductVariant
from app.modules.rating.public import SpeedClass
from app.modules.tournament.application.ports import (
    NotRegistered,
    PlayerDirectory,
    RegistrationDeadlinePassed,
    RegistrationNotOpen,
    RegistrationRepository,
    TournamentNotFound,
    TournamentRepository,
)
from app.modules.tournament.domain.events import (
    RegistrationClosed,
    RegistrationOpened,
    TournamentCreated,
)
from app.modules.tournament.domain.registration import Registration
from app.modules.tournament.domain.tournament import (
    Tournament,
    TournamentFormat,
    TournamentStatus,
)
from app.platform.events import DomainEvent
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class TournamentRegistrationService:
    """Create a tournament, open it, enter and withdraw players, close it."""

    def __init__(
        self,
        *,
        tournaments: TournamentRepository,
        registrations: RegistrationRepository,
        players: PlayerDirectory,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._tournaments = tournaments
        self._registrations = registrations
        self._players = players
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def create(
        self,
        *,
        name: str,
        variant: ProductVariant,
        speed_class: SpeedClass,
        capacity: int,
        rated: bool = True,
        created_by: UUID | None = None,
        registration_deadline: datetime | None = None,
    ) -> Tournament:
        """Creates a tournament in `DRAFT` — T-3.

        **System-facing**, with no HTTP surface in this epic: only
        administrators and the platform create tournaments in v0.x, and the
        admin module does not exist yet. `created_by=None` means the
        platform created it, which is the absence of a person rather than a
        sentinel.

        The format is not a parameter. v0.x runs one, and accepting it would
        let a caller ask for a tournament the aggregate then refuses —
        `Tournament.__post_init__` still checks, so the constant here is a
        convenience rather than the guarantee.
        """
        async with self._unit_of_work:
            tournament = await self._tournaments.create(
                Tournament(
                    id=uuid4(),
                    name=name,
                    format=TournamentFormat.SINGLE_ELIMINATION,
                    variant=variant,
                    speed_class=speed_class,
                    rated=rated,
                    capacity=capacity,
                    created_by=created_by,
                    created_at=self._clock.now(),
                    registration_deadline=registration_deadline,
                )
            )
            await self._events.publish(
                TournamentCreated(
                    occurred_at=tournament.created_at,
                    tournament_id=tournament.id,
                    name=tournament.name,
                    format=tournament.format.value,
                    capacity=tournament.capacity,
                )
            )
            await self._unit_of_work.commit()

        return tournament

    async def open_registration(self, tournament_id: UUID) -> Tournament:
        """`DRAFT` → `REGISTRATION_OPEN`."""
        return await self._transition(tournament_id, TournamentStatus.REGISTRATION_OPEN)

    async def close_registration(self, tournament_id: UUID) -> Tournament:
        """`REGISTRATION_OPEN` → `REGISTRATION_CLOSED`, by an operator.

        The manual half of §2's rule; the automatic half is
        `TournamentDeadlineTask`. Both end in the same state, and the
        aggregate's transition table refuses a second close either way.
        """
        return await self._transition(tournament_id, TournamentStatus.REGISTRATION_CLOSED)

    async def register(self, tournament_id: UUID, player_id: UUID) -> Registration:
        """Enters one player. Raises rather than returning a result type.

        Order matters and is the point of this method:

            player exists?   a published read, before any lock is taken
            lock the row     FOR UPDATE — the capacity mechanism
            open?            checked on the locked row, not a stale one
            count + insert   one transaction, under that lock
        """
        await self._require_player(player_id)

        async with self._unit_of_work:
            tournament = await self._tournaments.lock(tournament_id)
            if tournament is None:
                raise TournamentNotFound(f"no tournament {tournament_id}")
            if not tournament.is_open_for_registration:
                raise RegistrationNotOpen(f"this tournament is {tournament.status.value}")
            self._require_within_deadline(tournament)

            registration = await self._registrations.add(
                Registration(
                    tournament_id=tournament_id,
                    player_id=player_id,
                    registered_at=self._clock.now(),
                ),
                capacity=tournament.capacity,
            )
            await self._unit_of_work.commit()

        logger.info(
            "tournament_player_registered",
            extra={"tournament_id": str(tournament_id), "user_id": str(player_id)},
        )
        return registration

    async def withdraw(self, tournament_id: UUID, player_id: UUID) -> Registration:
        """Withdraws before registration closes — §4.

        After close the field is fixed: the bracket is built from exactly
        those players, and a withdrawal would leave a seat nothing fills.
        That is why this refuses rather than converting to a forfeit — a
        forfeit is a *match* outcome and there is no match yet.
        """
        async with self._unit_of_work:
            tournament = await self._tournaments.lock(tournament_id)
            if tournament is None:
                raise TournamentNotFound(f"no tournament {tournament_id}")
            if not tournament.is_open_for_registration:
                raise RegistrationNotOpen("registration has closed and the field is fixed")

            registration = await self._registrations.find(tournament_id, player_id)
            if registration is None or not registration.occupies_a_slot:
                raise NotRegistered("this player has no live registration")

            withdrawn = registration.withdrawn(self._clock.now())
            await self._registrations.withdraw(withdrawn)
            await self._unit_of_work.commit()

        return withdrawn

    async def entrant_count(self, tournament_id: UUID) -> int:
        """How many players currently occupy a slot. A read — no lock."""
        return await self._registrations.count_active(tournament_id)

    async def _transition(self, tournament_id: UUID, status: TournamentStatus) -> Tournament:
        async with self._unit_of_work:
            tournament = await self._tournaments.lock(tournament_id)
            if tournament is None:
                raise TournamentNotFound(f"no tournament {tournament_id}")

            moved = tournament.transitioned_to(status)
            await self._tournaments.save(moved)
            await self._events.publish(await self._announcement(moved))
            await self._unit_of_work.commit()

        return moved

    async def _announcement(self, tournament: Tournament) -> DomainEvent:
        at = self._clock.now()
        if tournament.status is TournamentStatus.REGISTRATION_OPEN:
            return RegistrationOpened(occurred_at=at, tournament_id=tournament.id)
        return RegistrationClosed(
            occurred_at=at,
            tournament_id=tournament.id,
            entrant_count=await self._registrations.count_active(tournament.id),
        )

    def _require_within_deadline(self, tournament: Tournament) -> None:
        """§2 — the deadline is the promise, not the sweep's tick.

        `TournamentDeadlineTask` closes an overdue tournament on its own
        schedule, so between the deadline passing and the next sweep the
        status still says `REGISTRATION_OPEN`. Without this check a player
        who arrived in that window would be admitted into a field the
        platform had already promised was closed — and the bracket would be
        seeded from entrants who beat a worker rather than a clock.

        Checked on the **locked** row, so a close landing concurrently
        cannot let a late entry through either.
        """
        deadline = tournament.registration_deadline
        if deadline is not None and self._clock.now() >= deadline:
            raise RegistrationDeadlinePassed(
                f"registration for this tournament closed at {deadline.isoformat()}"
            )

    async def _require_player(self, player_id: UUID) -> None:
        """§3 — participants are validated through `users.public`.

        `UserNotFound` propagates rather than being translated: it is
        already a domain error with a stable code, and re-wrapping it would
        hide which of the two ids in a registration request was wrong.
        """
        await self._players.get_profile(player_id)


class TournamentDeadlineService:
    """Closes registration for every tournament past its deadline — §2.

    Separate from `TournamentRegistrationService` because the two answer
    different questions and are driven by different things: one is a
    request about a named tournament, this is a sweep with no subject. A
    single class would hold a repository the request path never uses and a
    clock the sweep reads differently.

    Never raises. It runs from a schedule, and a sweep that propagated would
    stop the schedule that called it — the argument
    `ClockAdjudicationService.adjudicate_once` makes, and the same posture.
    """

    def __init__(
        self,
        *,
        tournaments: TournamentRepository,
        registrations: RegistrationRepository,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._tournaments = tournaments
        self._registrations = registrations
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def close_overdue(self) -> int:
        """One bounded sweep. Returns how many closed.

        Idempotent by predicate: a tournament already closed does not match
        the claim, so a redelivery or a second worker finds nothing. There
        is no ledger to keep — see `infrastructure/tasks.py`.
        """
        now = self._clock.now()

        try:
            async with self._unit_of_work:
                closed = await self._tournaments.close_overdue(now=now)
                for tournament_id in closed:
                    await self._events.publish(
                        RegistrationClosed(
                            occurred_at=now,
                            tournament_id=tournament_id,
                            entrant_count=await self._registrations.count_active(tournament_id),
                        )
                    )
                await self._unit_of_work.commit()
        except Exception as exc:  # noqa: BLE001 — a sweep must not stop its schedule
            logger.error(
                "tournament_deadline_sweep_failed",
                extra={"error": type(exc).__name__},
                exc_info=exc,
            )
            return 0

        if closed:
            logger.info("tournament_registration_closed", extra={"closed": len(closed)})
        return len(closed)


__all__ = ["TournamentDeadlineService", "TournamentRegistrationService"]
