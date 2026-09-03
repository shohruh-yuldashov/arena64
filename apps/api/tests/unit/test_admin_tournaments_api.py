"""The admin Tournaments read surface — A64-024.5 §24.

Five tests over the real handlers with in-memory directories. What is
asserted is the guard, the typed filters, what leaves the server, and the
one thing a tournament view gets wrong most easily: resolving a player per
entrant instead of once per page.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.core.identifiers import generate_uuid7
from app.modules.admin.presentation.routers.tournaments import (
    admin_tournaments_router,
    list_tournaments,
    read_tournament,
)
from app.modules.admin.presentation.schemas.tournaments import (
    AdminEntrantView,
    AdminPairingView,
    AdminStandingView,
    AdminTournamentSummary,
)
from app.modules.game.public.variants import ProductVariant
from app.modules.tournament.domain.registration import RegistrationStatus
from app.modules.tournament.domain.rounds import RoundStatus
from app.modules.tournament.domain.standings import FinalStatus
from app.modules.tournament.domain.tournament import TournamentFormat, TournamentStatus
from app.modules.tournament.public.administration import (
    AdminEntrant,
    AdminLiveTournamentSummary,
    AdminPairing,
    AdminRound,
    AdminStanding,
    AdminTournamentDetail,
    AdminTournamentFilters,
    AdminTournamentPage,
    AdminTournamentRecord,
)
from app.modules.users.public import AdminUserRecord

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _record(**overrides: object) -> AdminTournamentRecord:
    base = {
        "tournament_id": generate_uuid7(),
        "name": "Friday Blitz",
        "format": TournamentFormat.SINGLE_ELIMINATION,
        "variant": ProductVariant.RUSSIAN_8X8,
        "speed_class": "blitz",
        "status": TournamentStatus.COMPLETED,
        "rated": True,
        "capacity": 8,
        "entrant_count": 8,
        "registration_deadline": None,
        "started_at": NOW,
        "completed_at": NOW,
        "created_at": NOW,
    }
    base.update(overrides)
    return AdminTournamentRecord(**base)  # type: ignore[arg-type]


class InMemoryTournaments:
    def __init__(self, records: list[AdminTournamentRecord]) -> None:
        self.records = records
        self.seen: list[AdminTournamentFilters] = []
        self.detail: AdminTournamentDetail | None = None

    async def list_tournaments(
        self, *, filters: AdminTournamentFilters, limit: int, cursor: str | None
    ) -> AdminTournamentPage:
        self.seen.append(filters)
        rows = list(self.records)
        if filters.status is not None:
            rows = [row for row in rows if row.status == filters.status]
        if filters.rated is not None:
            rows = [row for row in rows if row.rated is filters.rated]
        return AdminTournamentPage(
            records=rows[:limit], next_cursor="c1" if len(rows) > limit else None
        )

    async def live_tournament_summary(self) -> AdminLiveTournamentSummary:
        return AdminLiveTournamentSummary(
            registration_open=sum(
                1 for row in self.records if row.status is TournamentStatus.REGISTRATION_OPEN
            ),
            in_progress=sum(
                1 for row in self.records if row.status is TournamentStatus.IN_PROGRESS
            ),
        )

    async def find_tournament(self, tournament_id: UUID) -> AdminTournamentDetail | None:
        return self.detail


class InMemoryAccounts:
    """Counts batch reads — the whole point of §14."""

    def __init__(self) -> None:
        self.batches: list[int] = []

    async def accounts_by_ids(self, user_ids: list[UUID]) -> dict[UUID, AdminUserRecord]:
        self.batches.append(len(user_ids))
        return {
            user_id: AdminUserRecord(
                id=user_id,
                username=f"p{index}",
                email=f"p{index}@example.com",
                display_name=None,
                is_active=True,
                is_verified=True,
                created_at=NOW,
            )
            for index, user_id in enumerate(user_ids)
        }

    async def list_accounts(self, **_: object) -> None:  # pragma: no cover
        raise AssertionError("the tournaments router must not list accounts")

    async def find_account(self, user_id: UUID) -> None:  # pragma: no cover
        raise AssertionError("the tournaments router must not read accounts one at a time")


class Headers:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


class _Identity:
    def __init__(self) -> None:
        self.id = generate_uuid7()


class TestTheListSurface:
    @pytest.mark.asyncio
    async def test_filters_reach_the_port_as_typed_values(self) -> None:
        """§7. Enums and booleans, so no free-text predicate reaches the
        database and nothing is post-filtered in the router."""
        tournaments = InMemoryTournaments(
            [_record(status=TournamentStatus.IN_PROGRESS), _record(rated=False)]
        )

        page = await list_tournaments(
            _Identity(),  # type: ignore[arg-type]
            tournaments,
            Headers(),  # type: ignore[arg-type]
            tournament_status=TournamentStatus.IN_PROGRESS,
            tournament_format=None,
            variant=None,
            rated=None,
            limit=25,
            cursor=None,
        )

        assert [item.status for item in page.items] == ["in_progress"]
        assert tournaments.seen[-1].status is TournamentStatus.IN_PROGRESS


class TestTheDetail:
    @pytest.mark.asyncio
    async def test_every_player_it_names_is_resolved_in_one_batch(self) -> None:
        """§14 — the N+1 a tournament view grows first.

        Entrants, standings and both seats of every pairing name the same
        people. A capacity-8 bracket has 7 nodes and 8 entrants, so the
        per-entrant implementation would issue upward of twenty reads.
        Asserted by counting: **one** batch, over the deduplicated set.
        """
        players = [generate_uuid7() for _ in range(8)]
        tournaments = InMemoryTournaments([])
        tournaments.detail = AdminTournamentDetail(
            tournament=_record(),
            entrants=[
                AdminEntrant(
                    player_id=player,
                    status=RegistrationStatus.REGISTERED,
                    seed_number=index + 1,
                    registered_at=NOW,
                    withdrawn_at=None,
                )
                for index, player in enumerate(players)
            ],
            rounds=[
                AdminRound(
                    round_number=1,
                    status=RoundStatus.COMPLETED,
                    published_at=NOW,
                    started_at=NOW,
                    completed_at=NOW,
                    pairing_count=4,
                )
            ],
            pairings=[
                AdminPairing(
                    round_number=1,
                    slot=slot,
                    light_player_id=players[slot * 2],
                    dark_player_id=players[slot * 2 + 1],
                    light_seed=slot * 2 + 1,
                    dark_seed=slot * 2 + 2,
                    winner_id=players[slot * 2],
                    advancement_reason="played",
                    match_ids=[generate_uuid7()],
                )
                for slot in range(4)
            ],
            standings=[
                AdminStanding(
                    player_id=player,
                    final_rank=index + 1,
                    seed_number=index + 1,
                    elimination_round=None,
                    eliminated_by_player_id=None,
                    wins=1,
                    losses=0,
                    draws=0,
                    final_status=FinalStatus.ELIMINATED,
                )
                for index, player in enumerate(players)
            ],
        )
        accounts = InMemoryAccounts()

        detail = await read_tournament(
            generate_uuid7(),
            _Identity(),  # type: ignore[arg-type]
            tournaments,
            accounts,  # type: ignore[arg-type]
            Headers(),  # type: ignore[arg-type]
        )

        assert len(accounts.batches) == 1
        # Eight distinct players, however many entrants, nodes and
        # standings name them.
        assert accounts.batches[0] == 8
        assert len(detail.entrants) == 8
        assert all(entrant.username is not None for entrant in detail.entrants)

    @pytest.mark.asyncio
    async def test_the_bracket_carries_coordinates_rather_than_a_drawn_tree(self) -> None:
        """§11 — correctness over decoration.

        The response publishes `(round_number, slot)` for every node, which
        is the domain's own identity for it: the parent is
        `(round_number + 1, slot // 2)`. Nothing here ships a second
        description of the edges that could disagree with
        `domain.bracket_plan`, and a console therefore cannot draw a tree
        the backend does not have.
        """
        assert "round_number" in AdminPairingView.model_fields
        assert "slot" in AdminPairingView.model_fields
        # And no invented edge fields, which is what a drawn tree would need.
        assert not {"next_slot", "parent_id", "feeds_into"} & set(AdminPairingView.model_fields)

    @pytest.mark.asyncio
    async def test_a_missing_tournament_answers_404(self) -> None:
        tournaments = InMemoryTournaments([])
        with pytest.raises(HTTPException) as missing:
            await read_tournament(
                generate_uuid7(),
                _Identity(),  # type: ignore[arg-type]
                tournaments,
                InMemoryAccounts(),  # type: ignore[arg-type]
                Headers(),  # type: ignore[arg-type]
            )
        assert missing.value.status_code == 404


class TestTheBoundary:
    def test_no_response_model_carries_private_participant_data(self) -> None:
        """§19 — an entrant list is not a mailing list.

        No email, no token, no session, no block state on any of the four
        models an operator sees. The console links to `/users/{id}` for
        anything the person's own page owns.
        """
        forbidden = {"email", "token", "session", "password_hash", "ip", "blocked"}
        for model in (
            AdminTournamentSummary,
            AdminEntrantView,
            AdminStandingView,
            AdminPairingView,
        ):
            assert not set(model.model_fields) & forbidden, model.__name__

    def test_every_route_is_guarded_and_no_route_writes_a_status(self) -> None:
        """§4, §23 — every route names the guard, and the shape is still safe.

        This asserted `GET` only until A64-024.5H, and it was right to: the
        console was read-only because `admin.audit_entry` was unbuilt. It
        exists, and §6.15 added four **named commands**.

        What still must hold is the property the original assertion was
        standing in for. There is no `PATCH` and no `PUT` here — a route
        that took a state would be one a caller could use to name
        `completed` or `cancelled`, and the whole design of that surface is
        that the transition is the route rather than a field.
        """
        from app.modules.admin.presentation.dependencies import require_admin

        methods: set[str] = set()
        for route in admin_tournaments_router.routes:
            methods |= getattr(route, "methods", set())
            dependant = getattr(route, "dependant", None)
            assert dependant is not None
            assert require_admin in {sub.call for sub in dependant.dependencies}

        assert methods <= {"GET", "HEAD", "POST"}, methods
