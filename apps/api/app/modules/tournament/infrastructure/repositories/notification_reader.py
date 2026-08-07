"""The adapter behind `tournament.public.TournamentNotificationReader`.

Database-only, per repositories.md §2. It answers two questions and cannot
ask a third: there is no method here that writes, and none that takes a
player, so the fan-out consumer holding it can neither change a bracket nor
be called in a loop.

## One statement per question, and that is the whole design

A64-019's capacity is 128 entrants. Every read below is written so that a
128-player tournament costs the same number of round trips as a 2-player
one — see `audience_of`, which joins the name onto the field rather than
fetching them separately.
"""

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tournament.domain.registration import RegistrationStatus
from app.modules.tournament.infrastructure.models import (
    RegistrationModel,
    StandingModel,
    TournamentModel,
)
from app.modules.tournament.public.notifications import (
    TournamentAudience,
    TournamentResults,
)


class SqlAlchemyTournamentNotificationReader:
    """Constructed per relay tick with that tick's session
    (repositories.md §5.1) — never holds one longer."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def audience_of(self, tournament_id: uuid.UUID) -> TournamentAudience | None:
        """The tournament's name and its live field. **Two statements.**

        Not one: an outer join of 128 registrations onto one tournament row
        repeats the name 128 times over the wire, and the second statement
        is an index scan on a primary key. The alternative optimises the
        cheaper half.

        The name is read first, so a tournament that does not exist costs
        one statement and no registration scan.
        """
        name = await self._session.scalar(
            select(TournamentModel.name).where(TournamentModel.id == tournament_id)
        )
        if name is None:
            return None

        # `REGISTERED` only — a withdrawn entry is excluded by the predicate
        # rather than by a filter the consumer must remember, which is what
        # makes "never notify somebody who left" a property of the query.
        participants = await self._session.scalars(
            select(RegistrationModel.player_id).where(
                RegistrationModel.tournament_id == tournament_id,
                RegistrationModel.status == RegistrationStatus.REGISTERED,
            )
        )
        return TournamentAudience(
            tournament_id=tournament_id,
            name=name,
            participant_ids=frozenset(participants),
        )

    async def results_of(self, tournament_id: uuid.UUID) -> TournamentResults | None:
        """The final placements. **Two statements**, for the reason above.

        An empty standings set returns `None` rather than a result with no
        placements: a completion event delivered before the standings were
        visible is a transient the caller must retry, and a `TournamentResults`
        with an empty mapping would be indistinguishable from a tournament
        nobody entered.
        """
        name = await self._session.scalar(
            select(TournamentModel.name).where(TournamentModel.id == tournament_id)
        )
        if name is None:
            return None

        rows = (
            await self._session.execute(
                select(
                    StandingModel.player_id,
                    StandingModel.final_rank,
                ).where(StandingModel.tournament_id == tournament_id)
            )
        ).all()
        if not rows:
            return None

        ranks = {row.player_id: row.final_rank for row in rows}
        # The champion is the rank-1 standing rather than a separate read:
        # `TournamentCompleted` carries `winner_id`, and a consumer that
        # wanted it has it on the event. Deriving it here would be a second
        # answer to a question already answered.
        winner = next((player for player, rank in ranks.items() if rank == 1), None)
        return TournamentResults(
            tournament_id=tournament_id,
            name=name,
            winner_id=winner,
            final_rank_by_player=ranks,
        )

    async def names_of(self, tournament_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, str]:
        """Several names in one statement. An empty request reads nothing."""
        if not tournament_ids:
            return {}

        rows = (
            await self._session.execute(
                select(TournamentModel.id, TournamentModel.name).where(
                    TournamentModel.id.in_(set(tournament_ids))
                )
            )
        ).all()
        return {row.id: row.name for row in rows}


__all__ = ["SqlAlchemyTournamentNotificationReader"]
