"""SQLAlchemy adapters for `tournament`'s two repositories.

## Two locks, opposite choices, both deliberate

`lock` uses `FOR UPDATE` **without** `SKIP LOCKED`: two players registering
at the same instant compete for the same slot, and skipping one would drop
a registration rather than serialising it.

`close_overdue` uses `FOR UPDATE SKIP LOCKED`: a tournament another worker
is already closing is one this worker should leave alone. Waiting would
make two workers take turns doing the same work.

The same split `game` already makes between a match write and a retention
sweep, and it is the reason both live here rather than behind one helper.
"""

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tournament.application.ports import (
    AlreadyRegistered,
    PlanAlreadyExists,
    TournamentIsFull,
)
from app.modules.tournament.domain.bracket_plan import BracketSlot, PersistedSeed
from app.modules.tournament.domain.registration import Registration, RegistrationStatus
from app.modules.tournament.domain.rounds import TournamentRound
from app.modules.tournament.domain.seeding import PlannedPairing, Seed
from app.modules.tournament.domain.tournament import Tournament, TournamentStatus
from app.modules.tournament.infrastructure.models import (
    PairingModel,
    RegistrationModel,
    TournamentModel,
    TournamentRoundModel,
)

_DUPLICATE_CONSTRAINT = "pk_registration"


class SqlAlchemyTournamentRepository:
    """`TournamentRepository` over one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, tournament: Tournament) -> Tournament:
        self._session.add(_to_model(tournament))
        await self._session.flush()
        return tournament

    async def by_id(self, tournament_id: uuid.UUID) -> Tournament | None:
        row = await self._session.get(TournamentModel, tournament_id)
        return _to_domain(row) if row else None

    async def lock(self, tournament_id: uuid.UUID) -> Tournament | None:
        """`FOR UPDATE`, no `SKIP LOCKED` — see this module's docstring."""
        row = await self._session.scalar(
            select(TournamentModel).where(TournamentModel.id == tournament_id).with_for_update()
        )
        return _to_domain(row) if row else None

    async def save(self, tournament: Tournament) -> None:
        row = await self._session.get(TournamentModel, tournament.id)
        if row is None:
            return
        row.status = tournament.status
        await self._session.flush()

    async def close_overdue(self, *, now: datetime) -> list[uuid.UUID]:
        """Closes every open tournament past its deadline. Idempotent.

        The predicate is the whole guard: a tournament already closed does
        not match, so a second worker — or a second run of the same one —
        finds nothing and does nothing. That is what makes the task safe to
        schedule rather than something to coordinate.
        """
        claimed = (
            select(TournamentModel)
            .where(
                TournamentModel.status == TournamentStatus.REGISTRATION_OPEN,
                TournamentModel.registration_deadline.is_not(None),
                TournamentModel.registration_deadline <= now,
            )
            .with_for_update(skip_locked=True)
        )

        closed: list[uuid.UUID] = []
        for row in (await self._session.scalars(claimed)).all():
            row.status = TournamentStatus.REGISTRATION_CLOSED
            closed.append(row.id)

        await self._session.flush()
        return closed


class SqlAlchemyRegistrationRepository:
    """`RegistrationRepository` over one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, registration: Registration, *, capacity: int) -> Registration:
        """Counts and inserts in one transaction, under the caller's lock.

        The count is **inside** the caller's `FOR UPDATE` on the tournament
        row, so two concurrent registrations serialise on that row and the
        second sees the first's insert. Counting outside the lock is
        precisely the check-then-insert §6 forbids.

        The duplicate is still the database's: the primary key refuses a
        second entry whatever its status, which is §4's no-re-registration
        rule made structural.
        """
        taken = await self.count_active(registration.tournament_id)
        if taken >= capacity:
            raise TournamentIsFull(f"this tournament is full ({taken}/{capacity})")

        self._session.add(
            RegistrationModel(
                tournament_id=registration.tournament_id,
                player_id=registration.player_id,
                status=registration.status,
                registered_at=registration.registered_at,
                withdrawn_at=registration.withdrawn_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as violation:
            if _DUPLICATE_CONSTRAINT not in str(violation.orig):
                raise
            raise AlreadyRegistered(
                "this player is already entered in this tournament"
            ) from violation

        return registration

    async def withdraw(self, registration: Registration) -> None:
        row = await self._session.get(
            RegistrationModel, (registration.tournament_id, registration.player_id)
        )
        if row is None:
            return
        row.status = registration.status
        row.withdrawn_at = registration.withdrawn_at
        await self._session.flush()

    async def find(self, tournament_id: uuid.UUID, player_id: uuid.UUID) -> Registration | None:
        row = await self._session.get(RegistrationModel, (tournament_id, player_id))
        if row is None:
            return None
        return Registration(
            tournament_id=row.tournament_id,
            player_id=row.player_id,
            registered_at=row.registered_at,
            status=row.status,
            withdrawn_at=row.withdrawn_at,
        )

    async def count_active(self, tournament_id: uuid.UUID) -> int:
        """Served by `ix_registration__active`, which is partial on the
        status — so it is an index over exactly the rows that count."""
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(RegistrationModel)
                .where(
                    RegistrationModel.tournament_id == tournament_id,
                    RegistrationModel.status == RegistrationStatus.REGISTERED,
                )
            )
            or 0
        )


def _to_model(tournament: Tournament) -> TournamentModel:
    return TournamentModel(
        id=tournament.id,
        name=tournament.name,
        format=tournament.format,
        variant=tournament.variant,
        speed_class=tournament.speed_class,
        status=tournament.status,
        rated=tournament.rated,
        capacity=tournament.capacity,
        created_by=tournament.created_by,
        registration_deadline=tournament.registration_deadline,
        created_at=tournament.created_at,
    )


def _to_domain(row: TournamentModel) -> Tournament:
    return Tournament(
        id=row.id,
        name=row.name,
        format=row.format,
        variant=row.variant,
        speed_class=row.speed_class,
        rated=row.rated,
        capacity=row.capacity,
        created_by=row.created_by,
        created_at=row.created_at,
        registration_deadline=row.registration_deadline,
        status=row.status,
    )


class SqlAlchemySeedRepository:
    """`SeedRepository` — the eligible field and its assigned seeds."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_entrants(self, tournament_id: uuid.UUID) -> list[uuid.UUID]:
        """Every player with a live registration — §2.

        Withdrawn entries are excluded by the status predicate; duplicates
        are impossible by the primary key. Ordered by `player_id` so the
        input to seeding is itself deterministic — the sort is total either
        way, but a stable input makes a failing test reproducible.
        """
        rows = await self._session.scalars(
            select(RegistrationModel.player_id)
            .where(
                RegistrationModel.tournament_id == tournament_id,
                RegistrationModel.status == RegistrationStatus.REGISTERED,
            )
            .order_by(RegistrationModel.player_id)
        )
        return list(rows)

    async def assign(self, tournament_id: uuid.UUID, seeds: list[Seed]) -> None:
        """Writes seed numbers onto the registrations — §4.

        One statement per seed rather than a bulk update, because the field
        is at most 128 and a `CASE` expression over it would be harder to
        read than the loop it replaces.
        """
        for seed in seeds:
            row = await self._session.get(RegistrationModel, (tournament_id, seed.player_id))
            if row is not None:
                row.seed_number = seed.number
        await self._session.flush()

    async def seeds_for(self, tournament_id: uuid.UUID) -> list[PersistedSeed]:
        """The persisted seeding, in seed order — §4.

        Returns `PersistedSeed`, which holds **only what is stored**. The
        earlier version returned the live `Seed` with `rating=0.0,
        deviation=0.0, is_provisional=False`, and those numbers read like
        measurements: a caller that trusted them would have reseeded a
        tournament to all-equal. A64-019.4 replaced the type rather than
        the values, so the absence is in the signature.
        """
        rows = await self._session.scalars(
            select(RegistrationModel)
            .where(
                RegistrationModel.tournament_id == tournament_id,
                RegistrationModel.seed_number.is_not(None),
            )
            .order_by(RegistrationModel.seed_number)
        )
        return [
            PersistedSeed(
                tournament_id=tournament_id,
                player_id=row.player_id,
                seed_number=row.seed_number or 0,
            )
            for row in rows
        ]


class SqlAlchemyPairingRepository:
    """`PairingRepository` — a round's slots, written once."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def plan_for(
        self, tournament_id: uuid.UUID, *, round_number: int
    ) -> list[PlannedPairing]:
        rows = await self._session.scalars(
            select(PairingModel)
            .where(
                PairingModel.tournament_id == tournament_id,
                PairingModel.round_number == round_number,
            )
            .order_by(PairingModel.slot)
        )
        return [
            PlannedPairing(
                round_number=row.round_number,
                slot=row.slot,
                light_player_id=row.light_player_id,
                dark_player_id=row.dark_player_id,
                light_seed=row.light_seed,
                dark_seed=row.dark_seed,
            )
            for row in rows
        ]

    async def save_plan(
        self, tournament_id: uuid.UUID, pairings: list[PlannedPairing]
    ) -> list[PlannedPairing]:
        """Writes a round's slots. Raises on a collision — §12.

        The primary key is the guard: two workers seeding at once cannot
        both insert, so the loser reads the winner's plan rather than
        overwriting it. Check-then-insert would let both through.
        """
        for pairing in pairings:
            self._session.add(
                PairingModel(
                    tournament_id=tournament_id,
                    round_number=pairing.round_number,
                    slot=pairing.slot,
                    light_player_id=pairing.light_player_id,
                    dark_player_id=pairing.dark_player_id,
                    light_seed=pairing.light_seed,
                    dark_seed=pairing.dark_seed,
                )
            )
        try:
            await self._session.flush()
        except IntegrityError as violation:
            if "pk_pairing" not in str(violation.orig):
                raise
            raise PlanAlreadyExists(
                f"round {pairings[0].round_number} is already planned"
            ) from violation

        return pairings


__all__ = [
    "SqlAlchemyBracketRepository",
    "SqlAlchemyPairingRepository",
    "SqlAlchemyRegistrationRepository",
    "SqlAlchemySeedRepository",
    "SqlAlchemyTournamentRepository",
]


class SqlAlchemyBracketRepository:
    """The materialised tree, and the compare-and-set that moves winners.

    ## Advancement is a conditional UPDATE, never read-then-write

    `SET winner_id = :w WHERE … AND winner_id IS NULL` — so two workers
    processing the same completed match cannot both succeed. The loser gets
    zero rows and re-reads: if the stored winner is the one it wanted, the
    work was done and it returns idempotently; if it differs, that is a
    genuine conflict and it says so.

    Read-then-write would let both through and the second would silently
    replace the first, which on a bracket means a player advancing out of a
    node they lost.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, tournament_id: uuid.UUID) -> bool:
        """Whether a bracket has already been materialised — §5's idempotency.

        Asked of the **round** relation, not the pairing one. Seeding
        (A64-019.3) already wrote round one's slots into `pairing`, so a
        pairing count is true the moment a tournament is seeded and would
        make materialisation a no-op that returns half a tree. The rounds
        are materialisation's own marker, written by nothing else.
        """
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(TournamentRoundModel)
                .where(TournamentRoundModel.tournament_id == tournament_id)
            )
        )

    async def materialise(
        self, tournament_id: uuid.UUID, nodes: list[BracketSlot], rounds: list[TournamentRound]
    ) -> None:
        """Writes the whole tree and its rounds. Raises on a collision.

        One flush, so a partial bracket is impossible: the caller's
        transaction either has every round and every node or none of them.
        """
        for round_ in rounds:
            self._session.add(
                TournamentRoundModel(
                    tournament_id=tournament_id,
                    round_number=round_.round_number,
                    status=round_.status,
                    published_at=round_.published_at,
                    started_at=round_.started_at,
                    completed_at=round_.completed_at,
                )
            )
        # **Only the rounds seeding did not write.** Round one's slots are
        # already in `pairing` from A64-019.3; re-inserting them would
        # collide with their own primary key. Their resolved byes are
        # applied afterwards by the caller's propagation pass.
        for node in nodes:
            if node.round_number == 1:
                continue
            self._session.add(
                PairingModel(
                    tournament_id=tournament_id,
                    round_number=node.round_number,
                    slot=node.slot,
                    light_player_id=node.light_player_id,
                    dark_player_id=node.dark_player_id,
                    light_seed=node.light_seed,
                    dark_seed=node.dark_seed,
                    winner_id=node.winner_id,
                )
            )

        try:
            await self._session.flush()
        except IntegrityError as violation:
            if "pk_pairing" not in str(violation.orig) and "pk_round" not in str(violation.orig):
                raise
            raise PlanAlreadyExists(
                f"tournament {tournament_id} already has a bracket"
            ) from violation

    async def nodes_for(self, tournament_id: uuid.UUID) -> list[BracketSlot]:
        rows = await self._session.scalars(
            select(PairingModel)
            .where(PairingModel.tournament_id == tournament_id)
            .order_by(PairingModel.round_number, PairingModel.slot)
        )
        return [_to_slot(row) for row in rows]

    async def claim_winner(
        self, tournament_id: uuid.UUID, *, round_number: int, slot: int, winner_id: uuid.UUID
    ) -> bool:
        """Sets the winner if there is none. Returns whether this call did it.

        `False` means somebody else got there first — the caller re-reads to
        decide whether that is agreement or a conflict. The guard is in the
        `WHERE`, so the decision is the database's.
        """
        # `cast`, because `AsyncSession.execute` is typed as returning
        # `Result` while an UPDATE always yields a `CursorResult` — and
        # `rowcount` is the whole point of this call.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(PairingModel)
                .where(
                    PairingModel.tournament_id == tournament_id,
                    PairingModel.round_number == round_number,
                    PairingModel.slot == slot,
                    PairingModel.winner_id.is_(None),
                )
                .values(winner_id=winner_id),
            ),
        )
        await self._session.flush()
        return bool(result.rowcount)

    async def fill_seat(
        self,
        tournament_id: uuid.UUID,
        *,
        round_number: int,
        slot: int,
        player_id: uuid.UUID,
        seed: int | None,
        light: bool,
    ) -> None:
        """Puts an advancing winner into a parent seat.

        Guarded on the seat being empty for the same reason `claim_winner`
        is: a retry must not overwrite a seat somebody else filled, and the
        seed travels with the player because the relation's check constraint
        pairs the two columns.
        """
        column = PairingModel.light_player_id if light else PairingModel.dark_player_id
        values = (
            {"light_player_id": player_id, "light_seed": seed}
            if light
            else {"dark_player_id": player_id, "dark_seed": seed}
        )
        await self._session.execute(
            update(PairingModel)
            .where(
                PairingModel.tournament_id == tournament_id,
                PairingModel.round_number == round_number,
                PairingModel.slot == slot,
                column.is_(None),
            )
            .values(**values)
        )
        await self._session.flush()


def _to_slot(row: PairingModel) -> BracketSlot:
    return BracketSlot(
        round_number=row.round_number,
        slot=row.slot,
        light_player_id=row.light_player_id,
        dark_player_id=row.dark_player_id,
        light_seed=row.light_seed,
        dark_seed=row.dark_seed,
        winner_id=row.winner_id,
        match_id=row.match_id,
    )
