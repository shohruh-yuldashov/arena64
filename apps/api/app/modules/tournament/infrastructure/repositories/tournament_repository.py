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
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tournament.application.ports import (
    AlreadyRegistered,
    AttemptAlreadyExists,
    PlanAlreadyExists,
    TournamentIsFull,
)
from app.modules.tournament.domain.attempts import AdvancementReason, PairingAttempt
from app.modules.tournament.domain.bracket_plan import (
    BracketSlot,
    LocatedNode,
    PersistedSeed,
)
from app.modules.tournament.domain.registration import Registration, RegistrationStatus
from app.modules.tournament.domain.rounds import TournamentRound
from app.modules.tournament.domain.seeding import PlannedPairing, Seed
from app.modules.tournament.domain.tournament import Tournament, TournamentStatus
from app.modules.tournament.infrastructure.models import (
    PairingAttemptModel,
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

    async def in_progress(self, *, limit: int) -> list[uuid.UUID]:
        """Up to `limit` tournaments being played, claimed for this worker.

        `FOR UPDATE SKIP LOCKED` and a ceiling, for the reasons
        `close_overdue` gives: a tournament another reconciler holds is one
        to leave alone, and an unbounded sweep is an outage waiting for
        enough tournaments. Ordered by id so a run is reproducible.
        """
        claimed = (
            select(TournamentModel.id)
            .where(TournamentModel.status == TournamentStatus.IN_PROGRESS)
            .order_by(TournamentModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(await self._session.scalars(claimed))

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
    """`PairingRepository` — a round's slots, written once.

    ## The insert runs inside a `SAVEPOINT`, and the retry path needs it to

    A collision here is the **expected** outcome of the race §12 describes:
    two workers compute an identical plan, one inserts, and the loser is
    supposed to re-read the winner's. But a failed statement poisons the
    enclosing transaction — PostgreSQL refuses every subsequent one until a
    rollback — so the loser's `plan_for` would raise `PendingRollbackError`
    instead of returning the plan, and the recovery the design describes
    would fail exactly when it was needed.

    `begin_nested()` scopes the rollback to the statement. The primary key
    is still the guard; what changes is that the loser can carry on. The
    same correction `SqlAlchemyPairingAttemptRepository.record` carries, for
    the same reason.
    """

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

        **Inside a `SAVEPOINT`**, and that is what makes "the loser reads
        the winner's plan" true rather than merely intended — see this
        class's docstring.
        """
        try:
            async with self._session.begin_nested():
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
                await self._session.flush()
        except IntegrityError as violation:
            if "pk_pairing" not in str(violation.orig):
                raise
            raise PlanAlreadyExists(
                f"round {pairings[0].round_number} is already planned"
            ) from violation

        return pairings


class SqlAlchemyPairingAttemptRepository:
    """`PairingAttemptRepository` — the matches played for a bracket node.

    ## Every guarantee here is a constraint, not a check

    `record` inserts and translates the violation; it does not read first.
    Two deliveries of one completed match both compute the same rematch, and
    only `uq_pairing_attempt__pairing_number` decides which one exists — a
    prior read would let both through, and two rematches for one pairing is
    two games two players did not agree to.

    `complete` is the same compare-and-set `claim_winner` uses, guarded on
    `outcome IS NULL`. The loser re-reads rather than overwriting: a result
    replaced by an identical-looking one is indistinguishable from a result
    replaced by a different one.

    ## The insert runs inside a `SAVEPOINT`, and it has to

    A collision here is **ordinary**, not exceptional: every retry of a
    launch reaches it, which is the whole point of insert-and-catch. But a
    failed statement poisons the enclosing transaction — PostgreSQL refuses
    every subsequent one until a rollback — so a caller that caught
    `AttemptAlreadyExists` and then read the winning row would find its
    session dead.

    `begin_nested()` scopes the rollback to the statement. The constraint is
    still the guard; what changes is that the loser can carry on and read
    what the winner wrote, which is the entire recovery path.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, attempt: PairingAttempt) -> PairingAttempt:
        """Writes a newly created attempt. Raises on a collision.

        The savepoint is not an optimisation — see this class's docstring on
        why the caller could not continue without it.
        """
        try:
            async with self._session.begin_nested():
                self._session.add(
                    PairingAttemptModel(
                        id=attempt.id,
                        pairing_id=attempt.pairing_id,
                        attempt_number=attempt.attempt_number,
                        match_id=attempt.match_id,
                        light_player_id=attempt.light_player_id,
                        dark_player_id=attempt.dark_player_id,
                        status=attempt.status,
                        outcome=attempt.outcome,
                        winner_id=attempt.winner_id,
                        completed_at=attempt.completed_at,
                        no_show_deadline=attempt.no_show_deadline,
                        light_present_at=attempt.light_present_at,
                        dark_present_at=attempt.dark_present_at,
                    )
                )
                await self._session.flush()
        except IntegrityError as violation:
            if not any(
                marker in str(violation.orig)
                for marker in ("uq_pairing_attempt__pairing_number", "uq_pairing_attempt__match")
            ):
                raise
            raise AttemptAlreadyExists(
                f"pairing {attempt.pairing_id} already has attempt {attempt.attempt_number}"
            ) from violation

        return attempt

    async def complete(self, attempt: PairingAttempt) -> bool:
        """Records a result if there is none. Returns whether this call did it."""
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(PairingAttemptModel)
                .where(
                    PairingAttemptModel.id == attempt.id,
                    PairingAttemptModel.outcome.is_(None),
                )
                .values(
                    status=attempt.status,
                    outcome=attempt.outcome,
                    winner_id=attempt.winner_id,
                    completed_at=attempt.completed_at,
                ),
            ),
        )
        await self._session.flush()
        return bool(result.rowcount)

    async def mark_present(
        self, match_id: uuid.UUID, player_id: uuid.UUID, *, at: datetime
    ) -> bool:
        """Records that a player reached this match. Returns whether it did.

        **One guarded statement, no read** — §6e. It runs on every gateway
        room join, including for matches no tournament owns, so a read
        first would put a `pairing_attempt` lookup on the WebSocket path for
        every game on the platform. A match this module does not own matches
        no row and the statement is a no-op.

        Guarded on `IS NULL`, so it records the **first** arrival and a
        reconnect is idempotent — which is exactly §6e's rule that a
        transient disconnect after somebody turned up is not a no-show.

        Both seats in one statement, because a player sits in one of them
        and which is not worth a branch: the `CASE` writes the seat that
        names them and leaves the other alone.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(PairingAttemptModel)
                .where(
                    PairingAttemptModel.match_id == match_id,
                    or_(
                        and_(
                            PairingAttemptModel.light_player_id == player_id,
                            PairingAttemptModel.light_present_at.is_(None),
                        ),
                        and_(
                            PairingAttemptModel.dark_player_id == player_id,
                            PairingAttemptModel.dark_present_at.is_(None),
                        ),
                    ),
                )
                .values(
                    light_present_at=case(
                        (PairingAttemptModel.light_player_id == player_id, at),
                        else_=PairingAttemptModel.light_present_at,
                    ),
                    dark_present_at=case(
                        (PairingAttemptModel.dark_player_id == player_id, at),
                        else_=PairingAttemptModel.dark_present_at,
                    ),
                )
            ),
        )
        await self._session.flush()
        return bool(result.rowcount)

    async def claim_no_show(self, *, now: datetime, limit: int) -> list[PairingAttempt]:
        """Up to `limit` unsettled attempts past their deadline, for this
        worker.

        `FOR UPDATE SKIP LOCKED`: an attempt another sweep already holds is
        one this worker should leave alone rather than wait for. Bounded,
        and ordered by deadline so the longest-waiting bracket moves first.

        Claiming is **not** deciding. The rows stay unsettled; the caller
        re-reads the authoritative match state and adjudicates in its own
        transaction, so a worker that dies between the two leaves attempts
        the next tick simply claims again.
        """
        claimed = (
            select(PairingAttemptModel)
            .where(
                PairingAttemptModel.outcome.is_(None),
                PairingAttemptModel.no_show_deadline.is_not(None),
                PairingAttemptModel.no_show_deadline <= now,
            )
            .order_by(PairingAttemptModel.no_show_deadline, PairingAttemptModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [_to_attempt(row) for row in (await self._session.scalars(claimed)).all()]

    async def by_match(self, match_id: uuid.UUID) -> PairingAttempt | None:
        row = await self._session.scalar(
            select(PairingAttemptModel).where(PairingAttemptModel.match_id == match_id)
        )
        return _to_attempt(row) if row else None

    async def for_pairings(self, pairing_ids: Sequence[uuid.UUID]) -> list[PairingAttempt]:
        if not pairing_ids:
            return []

        rows = await self._session.scalars(
            select(PairingAttemptModel)
            .where(PairingAttemptModel.pairing_id.in_(pairing_ids))
            .order_by(PairingAttemptModel.pairing_id, PairingAttemptModel.attempt_number)
        )
        return [_to_attempt(row) for row in rows]

    async def latest_for(self, pairing_id: uuid.UUID) -> PairingAttempt | None:
        row = await self._session.scalar(
            select(PairingAttemptModel)
            .where(PairingAttemptModel.pairing_id == pairing_id)
            .order_by(PairingAttemptModel.attempt_number.desc())
            .limit(1)
        )
        return _to_attempt(row) if row else None


def _to_attempt(row: PairingAttemptModel) -> PairingAttempt:
    return PairingAttempt(
        id=row.id,
        pairing_id=row.pairing_id,
        attempt_number=row.attempt_number,
        match_id=row.match_id,
        light_player_id=row.light_player_id,
        dark_player_id=row.dark_player_id,
        status=row.status,
        outcome=row.outcome,
        winner_id=row.winner_id,
        completed_at=row.completed_at,
        no_show_deadline=row.no_show_deadline,
        light_present_at=row.light_present_at,
        dark_present_at=row.dark_present_at,
    )


class SqlAlchemyRoundRepository:
    """`RoundRepository` — where each round is, never how it may move.

    The status machine lives in `domain/rounds.py`. A repository that
    decided transitions would be a second copy of the rule, and the copy is
    what goes stale.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def rounds_for(self, tournament_id: uuid.UUID) -> list[TournamentRound]:
        rows = await self._session.scalars(
            select(TournamentRoundModel)
            .where(TournamentRoundModel.tournament_id == tournament_id)
            .order_by(TournamentRoundModel.round_number)
        )
        return [
            TournamentRound(
                tournament_id=row.tournament_id,
                round_number=row.round_number,
                status=row.status,
                published_at=row.published_at,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
            for row in rows
        ]

    async def save(self, round_: TournamentRound) -> None:
        row = await self._session.get(
            TournamentRoundModel, (round_.tournament_id, round_.round_number)
        )
        if row is None:
            return
        row.status = round_.status
        row.published_at = round_.published_at
        row.started_at = round_.started_at
        row.completed_at = round_.completed_at
        await self._session.flush()


__all__ = [
    "SqlAlchemyBracketRepository",
    "SqlAlchemyPairingAttemptRepository",
    "SqlAlchemyPairingRepository",
    "SqlAlchemyRegistrationRepository",
    "SqlAlchemyRoundRepository",
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

        The flush is wrapped in a `SAVEPOINT` for the reason
        `SqlAlchemyPairingRepository.save_plan` records — the loser of the
        race is expected to re-read the winner's tree in this same session,
        and a poisoned transaction would refuse the read.
        """
        try:
            async with self._session.begin_nested():
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
                # **Only the rounds seeding did not write.** Round one's
                # slots are already in `pairing` from A64-019.3;
                # re-inserting them would collide with their own primary
                # key. Their resolved byes are applied afterwards by the
                # caller's propagation pass.
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
                            # Written with the winner, never after it: a bye
                            # chain can decide a later round at
                            # materialisation time, and
                            # `ck_pairing__reason_iff_winner` refuses one half.
                            advancement_reason=node.advancement_reason,
                        )
                    )
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

    async def locate(self, pairing_id: uuid.UUID) -> LocatedNode | None:
        """One node by its surrogate id — served by `uq_pairing__id`.

        The tournament travels with it because a completion carries only the
        opaque reference: without this, "which bracket does this match
        belong to" would need a scan.
        """
        row = await self._session.scalar(select(PairingModel).where(PairingModel.id == pairing_id))
        if row is None:
            return None
        return LocatedNode(tournament_id=row.tournament_id, node=_to_slot(row))

    async def claim_winner(
        self,
        tournament_id: uuid.UUID,
        *,
        round_number: int,
        slot: int,
        winner_id: uuid.UUID,
        reason: AdvancementReason,
    ) -> bool:
        """Sets the winner if there is none. Returns whether this call did it.

        `False` means somebody else got there first — the caller re-reads to
        decide whether that is agreement or a conflict. The guard is in the
        `WHERE`, so the decision is the database's.

        The reason goes in the same statement: `ck_pairing__reason_iff_winner`
        refuses one without the other, and a second write would be a window
        in which the bracket says somebody advanced and cannot say why.
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
                .values(winner_id=winner_id, advancement_reason=reason),
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
        advancement_reason=row.advancement_reason,
        id=row.id,
    )
