"""`AdministrativeTournamentDirectory` over PostgreSQL — A64-024.5.

Read-only: no `update`, no `delete`, no flush. A compromised admin
transport could enumerate tournaments and change none of them.

## Query shape

    list    2 statements — the page, then one grouped count of entrants
            for exactly the ids on it
    detail  5 statements — the tournament, its registrations, its rounds,
            its pairings, its standings; plus one for the matches those
            pairings produced

Every one is a set read over a bounded collection. §14 forbids the shape
this would naturally grow into — a count per row, a profile per entrant, a
match per pairing — and none of those loops exists here.

A tournament is bounded by `capacity`, so the detail's collections are
O(capacity) rather than unbounded. That is what makes one response
defensible instead of four endpoints.
"""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.modules.tournament.infrastructure.models import (
    PairingAttemptModel,
    PairingModel,
    RegistrationModel,
    StandingModel,
    TournamentModel,
    TournamentRoundModel,
)
from app.modules.tournament.public.administration import (
    AdminEntrant,
    AdminPairing,
    AdminRound,
    AdminStanding,
    AdminTournamentDetail,
    AdminTournamentFilters,
    AdminTournamentPage,
    AdminTournamentRecord,
)


class SqlAlchemyAdministrativeTournamentDirectory:
    """Tournaments, for the admin console."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tournaments(
        self, *, filters: AdminTournamentFilters, limit: int, cursor: str | None
    ) -> AdminTournamentPage:
        statement = select(TournamentModel)

        if filters.status is not None:
            statement = statement.where(TournamentModel.status == filters.status)
        if filters.format is not None:
            statement = statement.where(TournamentModel.format == filters.format)
        if filters.variant is not None:
            statement = statement.where(TournamentModel.variant == filters.variant)
        if filters.rated is not None:
            statement = statement.where(TournamentModel.rated.is_(filters.rated))

        if cursor is not None:
            after = _TournamentCursor.decode(cursor)
            statement = statement.where(
                tuple_(TournamentModel.created_at, TournamentModel.id)
                < tuple_(literal(after.created_at), literal(after.tournament_id))
            )

        rows = (
            (
                await self._session.execute(
                    statement.order_by(
                        TournamentModel.created_at.desc(), TournamentModel.id.desc()
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )

        has_more = len(rows) > limit
        page = list(rows[:limit])
        counts = await self._entrant_counts([row.id for row in page])

        next_cursor = (
            _TournamentCursor(created_at=page[-1].created_at, tournament_id=page[-1].id).encode()
            if has_more and page
            else None
        )
        return AdminTournamentPage(
            records=[_to_record(row, counts.get(row.id, 0)) for row in page],
            next_cursor=next_cursor,
        )

    async def find_tournament(self, tournament_id: UUID) -> AdminTournamentDetail | None:
        row = await self._session.get(TournamentModel, tournament_id)
        if row is None:
            return None

        counts = await self._entrant_counts([tournament_id])
        entrants = await self._entrants(tournament_id)
        rounds = await self._rounds(tournament_id)
        pairings = await self._pairings(tournament_id)
        standings = await self._standings(tournament_id)

        return AdminTournamentDetail(
            tournament=_to_record(row, counts.get(tournament_id, 0)),
            entrants=entrants,
            rounds=rounds,
            pairings=pairings,
            standings=standings,
        )

    async def _entrant_counts(self, tournament_ids: Sequence[UUID]) -> dict[UUID, int]:
        """How many entrants each tournament has, in **one** grouped read.

        A count per row is the N+1 §14 names, and it is the one this list
        would grow first: `len(tournament.registrations)` is the obvious
        expression and the expensive one.
        """
        if not tournament_ids:
            return {}
        rows = await self._session.execute(
            select(RegistrationModel.tournament_id, func.count())
            .where(RegistrationModel.tournament_id.in_(set(tournament_ids)))
            .group_by(RegistrationModel.tournament_id)
        )
        return {tournament_id: count for tournament_id, count in rows.all()}

    async def _entrants(self, tournament_id: UUID) -> list[AdminEntrant]:
        rows = (
            (
                await self._session.execute(
                    select(RegistrationModel)
                    .where(RegistrationModel.tournament_id == tournament_id)
                    .order_by(
                        RegistrationModel.seed_number.nulls_last(),
                        RegistrationModel.registered_at,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            AdminEntrant(
                player_id=row.player_id,
                status=row.status,
                seed_number=row.seed_number,
                registered_at=row.registered_at,
                withdrawn_at=row.withdrawn_at,
            )
            for row in rows
        ]

    async def _rounds(self, tournament_id: UUID) -> list[AdminRound]:
        """Rounds, with how many nodes each holds.

        The pairing count comes from one grouped read rather than a query
        per round — the same rule the entrant count follows.
        """
        rounds = (
            (
                await self._session.execute(
                    select(TournamentRoundModel)
                    .where(TournamentRoundModel.tournament_id == tournament_id)
                    .order_by(TournamentRoundModel.round_number)
                )
            )
            .scalars()
            .all()
        )
        counted: dict[int, int] = {
            round_number: count
            for round_number, count in (
                await self._session.execute(
                    select(PairingModel.round_number, func.count())
                    .where(PairingModel.tournament_id == tournament_id)
                    .group_by(PairingModel.round_number)
                )
            ).all()
        }
        return [
            AdminRound(
                round_number=row.round_number,
                status=row.status,
                published_at=row.published_at,
                started_at=row.started_at,
                completed_at=row.completed_at,
                pairing_count=counted.get(row.round_number, 0),
            )
            for row in rounds
        ]

    async def _pairings(self, tournament_id: UUID) -> list[AdminPairing]:
        """Every bracket node, with the matches it produced.

        The matches are collected in **one** read over
        `pairing_attempt` and indexed by pairing — a query per node is the
        N+1 a bracket view grows most naturally.
        """
        rows = (
            (
                await self._session.execute(
                    select(PairingModel)
                    .where(PairingModel.tournament_id == tournament_id)
                    .order_by(PairingModel.round_number, PairingModel.slot)
                )
            )
            .scalars()
            .all()
        )

        attempts = (
            await self._session.execute(
                select(PairingAttemptModel.pairing_id, PairingAttemptModel.match_id)
                .where(PairingAttemptModel.pairing_id.in_([row.id for row in rows] or [None]))
                .order_by(PairingAttemptModel.attempt_number)
            )
        ).all()
        by_pairing: dict[UUID, list[UUID]] = {}
        for pairing_id, match_id in attempts:
            by_pairing.setdefault(pairing_id, []).append(match_id)

        return [
            AdminPairing(
                round_number=row.round_number,
                slot=row.slot,
                light_player_id=row.light_player_id,
                dark_player_id=row.dark_player_id,
                light_seed=row.light_seed,
                dark_seed=row.dark_seed,
                winner_id=row.winner_id,
                advancement_reason=(
                    row.advancement_reason.value if row.advancement_reason else None
                ),
                match_ids=by_pairing.get(row.id, []),
            )
            for row in rows
        ]

    async def _standings(self, tournament_id: UUID) -> list[AdminStanding]:
        """Final placements, **as `tournament` computed them**.

        Read, never recomputed: §13 forbids a second standings algorithm,
        and a console that derived placements from matches would be a
        second source of truth for who won.
        """
        rows = (
            (
                await self._session.execute(
                    select(StandingModel)
                    .where(StandingModel.tournament_id == tournament_id)
                    .order_by(StandingModel.final_rank)
                )
            )
            .scalars()
            .all()
        )
        return [
            AdminStanding(
                player_id=row.player_id,
                final_rank=row.final_rank,
                seed_number=row.seed_number,
                elimination_round=row.elimination_round,
                eliminated_by_player_id=row.eliminated_by_player_id,
                wins=row.wins,
                losses=row.losses,
                draws=row.draws,
                final_status=row.final_status,
            )
            for row in rows
        ]


def _to_record(row: TournamentModel, entrants: int) -> AdminTournamentRecord:
    """One row as the published record, field by field.

    Not by reflection: a column added to `TournamentModel` must not
    silently become something the admin console publishes.
    """
    return AdminTournamentRecord(
        tournament_id=row.id,
        name=row.name,
        format=row.format,
        variant=row.variant,
        speed_class=row.speed_class.value if hasattr(row.speed_class, "value") else row.speed_class,
        status=row.status,
        rated=row.rated,
        capacity=row.capacity,
        entrant_count=entrants,
        registration_deadline=row.registration_deadline,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
    )


@dataclass(frozen=True, slots=True)
class _TournamentCursor:
    """The keyset position, opaque on the wire.

    An unparseable cursor **raises** rather than silently starting from the
    beginning — "page four quietly became page one" is the bug nobody
    reports.
    """

    created_at: datetime
    tournament_id: UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.tournament_id}"
        return urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str) -> "_TournamentCursor":
        padding = "=" * (-len(cursor) % 4)
        try:
            raw = urlsafe_b64decode(cursor + padding).decode()
            moment, identifier = raw.split("|", 1)
            return cls(created_at=datetime.fromisoformat(moment), tournament_id=UUID(identifier))
        except (ValueError, TypeError) as exc:
            raise ValidationError("That page cursor could not be read.") from exc


__all__ = ["SqlAlchemyAdministrativeTournamentDirectory"]
