"""The read side — SPEC-TOURNAMENT §6g, A64-019.6 §9–§12.

Four questions, four statements, and none of them derives anything: a
completed tournament's placement was materialised once (§6f) and this reads
the rows. A projection that recomputed per request would make a published
result depend on code that can change, and the bracket it came from is
already terminal.

## Why the reads live in their own adapter

`tournament_repository.py` holds the **write** model — locks,
compare-and-set, insert-and-catch. Nothing here takes a lock or writes
anything, and separating them is what makes that checkable: a read path that
could reach `claim_winner` is one that could move a bracket while somebody
is looking at it.

## Bounded, and keyset where it matters

A bracket and a standings list are bounded by the field size (≤ 128, T-2),
so both are read whole. A player's tournament history is **not** bounded,
so it pages by `(registered_at, tournament_id)` descending — never `OFFSET`,
whose cost grows with the page number and whose results shift when a row is
inserted mid-scan.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, and_, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tournament.application.read_models import (
    AttemptSummary,
    BracketNodeView,
    BracketView,
    HistoryCursor,
    PlayerTournamentEntry,
    PlayerTournamentPage,
    RoundView,
    StandingView,
    TournamentSummary,
)
from app.modules.tournament.domain.registration import RegistrationStatus
from app.modules.tournament.domain.rounds import RoundStatus
from app.modules.tournament.infrastructure.models import (
    PairingAttemptModel,
    PairingModel,
    RegistrationModel,
    StandingModel,
    TournamentModel,
    TournamentRoundModel,
)


class SqlAlchemyTournamentResults:
    """Every public read a tournament answers, over one session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def summary(self, tournament_id: uuid.UUID) -> TournamentSummary | None:
        """One tournament's public detail — §9. `None` when there is none.

        `None` rather than a raise, and the route turns it into `404`: §7's
        rule is that an invisible resource answers exactly as one that was
        never created, and a reader here cannot tell the two apart either.
        """
        row = await self._session.get(TournamentModel, tournament_id)
        if row is None:
            return None
        return TournamentSummary(
            id=row.id,
            name=row.name,
            format=row.format,
            variant=row.variant.value,
            speed_class=row.speed_class.value,
            rated=row.rated,
            capacity=row.capacity,
            status=row.status,
            entrant_count=await self._entrant_count(tournament_id),
            current_round=await self._current_round(tournament_id),
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )

    async def bracket(self, tournament_id: uuid.UUID) -> BracketView:
        """The whole bracket, round by round — §10.

        Read whole rather than paged: a field is at most 128 (T-2), so a
        bracket is at most 127 nodes and rendering one needs all of them.

        Two statements and a join in memory, because the alternative — a
        `JOIN` returning one row per attempt — would repeat every node's
        columns per attempt and need de-duplicating on the way out.
        """
        nodes = list(
            await self._session.scalars(
                select(PairingModel)
                .where(PairingModel.tournament_id == tournament_id)
                .order_by(PairingModel.round_number, PairingModel.slot)
            )
        )
        attempts = await self._attempts_by_pairing([node.id for node in nodes])
        statuses = await self._round_statuses(tournament_id)

        by_round: dict[int, list[BracketNodeView]] = {}
        for node in nodes:
            by_round.setdefault(node.round_number, []).append(
                _node_view(node, attempts.get(node.id, ()))
            )

        return BracketView(
            tournament_id=tournament_id,
            rounds=tuple(
                RoundView(
                    round_number=number,
                    status=statuses.get(number, RoundStatus.PENDING),
                    nodes=tuple(by_round[number]),
                )
                for number in sorted(by_round)
            ),
        )

    async def standings(self, tournament_id: uuid.UUID) -> list[StandingView]:
        """The published placement — §11, served by `ix_standing__placement`.

        Empty for a tournament that has not completed, which is the honest
        answer: standings are materialised at completion (§6f), so there is
        no partial placement to show and nothing here invents one.
        """
        rows = await self._session.scalars(
            select(StandingModel)
            .where(StandingModel.tournament_id == tournament_id)
            .order_by(
                StandingModel.final_rank,
                StandingModel.seed_number,
                StandingModel.player_id,
            )
        )
        return [
            StandingView(
                player_id=row.player_id,
                final_rank=row.final_rank,
                seed_number=row.seed_number,
                wins=row.wins,
                losses=row.losses,
                draws=row.draws,
                adjudicated_advancements=row.adjudicated_advancements,
                final_status=row.final_status,
                elimination_round=row.elimination_round,
                eliminated_by_player_id=row.eliminated_by_player_id,
            )
            for row in rows
        ]

    async def player_history(
        self, player_id: uuid.UUID, *, after: HistoryCursor | None, limit: int
    ) -> PlayerTournamentPage:
        """A player's tournaments, newest first — §12.

        **Keyset, never `OFFSET`.** The order is
        `(registered_at, tournament_id)` descending, both keys, because a
        single-key order over an unbounded history pages unstably the moment
        two rows share an instant — a row seen twice or skipped entirely.

        `limit + 1` rows are read so the presence of a next page is a fact
        rather than a second `COUNT`.
        """
        statement = (
            select(RegistrationModel, TournamentModel, StandingModel)
            .join(TournamentModel, TournamentModel.id == RegistrationModel.tournament_id)
            .outerjoin(
                StandingModel,
                and_(
                    StandingModel.tournament_id == RegistrationModel.tournament_id,
                    StandingModel.player_id == RegistrationModel.player_id,
                ),
            )
            .where(RegistrationModel.player_id == player_id)
            .order_by(
                RegistrationModel.registered_at.desc(),
                RegistrationModel.tournament_id.desc(),
            )
            .limit(limit + 1)
        )
        rows = (await self._session.execute(_after(statement, after))).all()

        entries = [
            PlayerTournamentEntry(
                tournament=TournamentSummary(
                    id=tournament.id,
                    name=tournament.name,
                    format=tournament.format,
                    variant=tournament.variant.value,
                    speed_class=tournament.speed_class.value,
                    rated=tournament.rated,
                    capacity=tournament.capacity,
                    status=tournament.status,
                    entrant_count=await self._entrant_count(tournament.id),
                    current_round=await self._current_round(tournament.id),
                    created_at=tournament.created_at,
                    started_at=tournament.started_at,
                    completed_at=tournament.completed_at,
                ),
                seed_number=registration.seed_number,
                final_rank=standing.final_rank if standing else None,
                final_status=standing.final_status if standing else None,
            )
            for registration, tournament, standing in rows[:limit]
        ]

        cursor = (
            HistoryCursor(
                registered_at=rows[limit - 1][0].registered_at,
                tournament_id=rows[limit - 1][0].tournament_id,
            )
            if len(rows) > limit
            else None
        )
        return PlayerTournamentPage(entries=tuple(entries), next_cursor=cursor)

    async def _entrant_count(self, tournament_id: uuid.UUID) -> int:
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

    async def _current_round(self, tournament_id: uuid.UUID) -> int | None:
        """The round being played, or `None`.

        The lowest round that is published or in progress — a bracket is
        played bottom-up, so the earliest unfinished layer is the one a
        client renders as "now".
        """
        current = await self._session.scalar(
            select(func.min(TournamentRoundModel.round_number)).where(
                TournamentRoundModel.tournament_id == tournament_id,
                TournamentRoundModel.status.in_((RoundStatus.PUBLISHED, RoundStatus.IN_PROGRESS)),
            )
        )
        return int(current) if current is not None else None

    async def _round_statuses(self, tournament_id: uuid.UUID) -> dict[int, RoundStatus]:
        rows = await self._session.execute(
            select(TournamentRoundModel.round_number, TournamentRoundModel.status).where(
                TournamentRoundModel.tournament_id == tournament_id
            )
        )
        return {number: status for number, status in rows.all()}

    async def _attempts_by_pairing(
        self, pairing_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[AttemptSummary, ...]]:
        """Every node's attempts, in one statement.

        Batched rather than per node, because a bracket read that issued one
        query per node would be the N+1 on the platform's largest read.
        """
        if not pairing_ids:
            return {}

        rows = await self._session.scalars(
            select(PairingAttemptModel)
            .where(PairingAttemptModel.pairing_id.in_(pairing_ids))
            .order_by(PairingAttemptModel.pairing_id, PairingAttemptModel.attempt_number)
        )

        grouped: dict[uuid.UUID, list[AttemptSummary]] = {}
        for row in rows:
            grouped.setdefault(row.pairing_id, []).append(
                AttemptSummary(
                    attempt_number=row.attempt_number,
                    match_id=row.match_id,
                    light_player_id=row.light_player_id,
                    dark_player_id=row.dark_player_id,
                    status=row.status,
                    outcome=row.outcome,
                    winner_id=row.winner_id,
                )
            )
        return {pairing_id: tuple(items) for pairing_id, items in grouped.items()}


def _after(statement: Select, cursor: HistoryCursor | None) -> Select:  # type: ignore[type-arg]
    """The keyset predicate, or the statement unchanged for a first page.

    A **row comparison** rather than an `OR` of two conditions, because
    `(a, b) < (:a, :b)` is the form PostgreSQL can serve directly from a
    composite index — the hand-expanded version is the same rows and a
    different plan.
    """
    if cursor is None:
        return statement
    return statement.where(
        tuple_(RegistrationModel.registered_at, RegistrationModel.tournament_id)
        < (cursor.registered_at, cursor.tournament_id)
    )


def _node_view(node: PairingModel, attempts: tuple[AttemptSummary, ...]) -> BracketNodeView:
    return BracketNodeView(
        pairing_id=node.id,
        round_number=node.round_number,
        slot=node.slot,
        light_player_id=node.light_player_id,
        dark_player_id=node.dark_player_id,
        light_seed=node.light_seed,
        dark_seed=node.dark_seed,
        winner_id=node.winner_id,
        advancement_reason=node.advancement_reason,
        attempts=attempts,
    )


__all__ = ["SqlAlchemyTournamentResults"]
