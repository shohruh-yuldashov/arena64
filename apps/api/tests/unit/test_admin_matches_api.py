"""The admin Matches read surface — A64-024.4 §18.

Five tests over the **real** router handlers with in-memory directories.
What is asserted is the contract an operator and an attacker each see: the
guard, the filters, what leaves the server, and — the one this endpoint
would most naturally get wrong — how many reads a page costs.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.core.identifiers import generate_uuid7
from app.modules.admin.presentation.routers.matches import (
    MAX_PAGE_SIZE,
    admin_matches_router,
    list_matches,
    read_match,
)
from app.modules.admin.presentation.schemas.matches import (
    AdminMatchDetail,
    AdminMatchPageResponse,
    AdminMatchParticipant,
    AdminMatchSummary,
)
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.variants import MatchOrigin, ProductVariant
from app.modules.game.public import AdminMatchFilters, AdminMatchPage, AdminMatchRecord
from app.modules.game.public.administration import AdminLiveMatchSummary
from app.modules.users.public import AdminUserRecord

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _match(**overrides: object) -> AdminMatchRecord:
    base = {
        "match_id": generate_uuid7(),
        "status": MatchRecordStatus.COMPLETED,
        "variant": ProductVariant.RUSSIAN_8X8,
        "rated": True,
        "origin": MatchOrigin.QUEUE,
        "light_player_id": generate_uuid7(),
        "dark_player_id": generate_uuid7(),
        "outcome": None,
        "termination_reason": None,
        "winner": None,
        "time_control": None,
        "speed_class": "blitz",
        "ply_number": 24,
        "created_at": NOW,
        "settled_at": NOW,
        "ended_at": NOW,
    }
    base.update(overrides)
    return AdminMatchRecord(**base)  # type: ignore[arg-type]


class InMemoryMatches:
    def __init__(self, records: list[AdminMatchRecord]) -> None:
        self.records = records
        self.calls = 0
        self.seen: list[AdminMatchFilters] = []

    async def list_matches(
        self, *, filters: AdminMatchFilters, limit: int, cursor: str | None
    ) -> AdminMatchPage:
        self.calls += 1
        self.seen.append(filters)
        rows = list(self.records)
        if filters.status is not None:
            rows = [row for row in rows if row.status == filters.status]
        if filters.rated is not None:
            rows = [row for row in rows if row.rated is filters.rated]
        if filters.participant_id is not None:
            rows = [
                row
                for row in rows
                if filters.participant_id in (row.light_player_id, row.dark_player_id)
            ]
        return AdminMatchPage(records=rows[:limit], next_cursor="c1" if len(rows) > limit else None)

    async def live_match_summary(self) -> AdminLiveMatchSummary:
        self.calls += 1
        return AdminLiveMatchSummary(
            active=sum(1 for row in self.records if row.status is MatchRecordStatus.ACTIVE),
            awaiting_acceptance=sum(
                1 for row in self.records if row.status is MatchRecordStatus.PENDING_ACCEPTANCE
            ),
        )

    async def find_match(self, match_id: UUID) -> AdminMatchRecord | None:
        self.calls += 1
        return next((row for row in self.records if row.match_id == match_id), None)


class InMemoryAccounts:
    """Counts batch reads, which is the whole point — §8."""

    def __init__(self, known: dict[UUID, str]) -> None:
        self.known = known
        self.batches: list[int] = []

    async def accounts_by_ids(self, user_ids: list[UUID]) -> dict[UUID, AdminUserRecord]:
        self.batches.append(len(user_ids))
        return {
            user_id: AdminUserRecord(
                id=user_id,
                username=name,
                email=f"{name}@example.com",
                display_name=None,
                is_active=True,
                is_verified=True,
                created_at=NOW,
            )
            for user_id, name in self.known.items()
            if user_id in set(user_ids)
        }

    async def list_accounts(self, **_: object) -> None:  # pragma: no cover — unused here
        raise AssertionError("the matches router must not list accounts")

    async def find_account(self, user_id: UUID) -> None:  # pragma: no cover
        raise AssertionError("the matches router must not read accounts one at a time")


class Headers:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


class _Identity:
    def __init__(self) -> None:
        self.id = generate_uuid7()


async def _list(
    matches: InMemoryMatches, accounts: InMemoryAccounts, **kwargs: object
) -> AdminMatchPageResponse:
    return await list_matches(
        _Identity(),  # type: ignore[arg-type]
        matches,
        accounts,  # type: ignore[arg-type]
        Headers(),  # type: ignore[arg-type]
        match_status=kwargs.get("match_status"),  # type: ignore[arg-type]
        rated=kwargs.get("rated"),  # type: ignore[arg-type]
        variant=None,
        origin=None,
        participant_id=kwargs.get("participant_id"),  # type: ignore[arg-type]
        limit=kwargs.get("limit", 25),  # type: ignore[arg-type]
        cursor=None,
    )


class TestTheListSurface:
    @pytest.mark.asyncio
    async def test_a_page_costs_one_match_read_and_one_batch_of_names(self) -> None:
        """§8 — the N+1 this endpoint would naturally grow.

        A fifty-row page names up to a hundred players, and the obvious
        implementation resolves each seat as it renders it. Asserted by
        counting: one match read, **one** batch, and the batch asks for the
        deduplicated set rather than one entry per seat.
        """
        shared = generate_uuid7()
        records = [_match(light_player_id=shared) for _ in range(MAX_PAGE_SIZE)]
        matches = InMemoryMatches(records)
        accounts = InMemoryAccounts({shared: "regular"})

        page = await _list(matches, accounts, limit=MAX_PAGE_SIZE)

        assert len(page.items) == MAX_PAGE_SIZE
        assert matches.calls == 1
        assert len(accounts.batches) == 1
        # `shared` appears in every row and is asked for once: fifty dark
        # seats plus one light.
        assert accounts.batches[0] == MAX_PAGE_SIZE + 1

    @pytest.mark.asyncio
    async def test_filters_reach_the_port_as_typed_values(self) -> None:
        """§6. Every filter is an enum or a boolean, so there is no
        free-text predicate reaching the database and nothing is
        post-filtered in the router."""
        player = generate_uuid7()
        matches = InMemoryMatches(
            [
                _match(status=MatchRecordStatus.ACTIVE, rated=False),
                _match(status=MatchRecordStatus.COMPLETED, light_player_id=player),
            ]
        )
        accounts = InMemoryAccounts({player: "subject"})

        active = await _list(matches, accounts, match_status=MatchRecordStatus.ACTIVE)
        assert [item.status for item in active.items] == ["active"]

        theirs = await _list(matches, accounts, participant_id=player)
        assert len(theirs.items) == 1
        assert matches.seen[-1].participant_id == player

    @pytest.mark.asyncio
    async def test_an_unresolvable_participant_keeps_its_id(self) -> None:
        """An erased account is a real state, not a gap.

        The seat still renders — the id is a fact the match row holds — and
        the name is `None` rather than a fabricated placeholder.
        """
        matches = InMemoryMatches([_match()])
        page = await _list(matches, InMemoryAccounts({}))

        seat = page.items[0].light
        assert seat.username is None
        assert seat.player_id is not None


class TestWhatLeavesTheServer:
    def test_no_response_model_can_carry_sensitive_participant_data(self) -> None:
        """§11 — a match page is not a database dump.

        The participant model is an id, a name and a side. There is no
        field for an email, an IP, a device, a session or a token, so no
        serialisation path could carry one — and the console links to
        `/users/{id}` for anything more, which is a page with its own
        decision about what to show.
        """
        forbidden = {
            "email",
            "ip",
            "ip_address",
            "device",
            "session",
            "session_id",
            "token",
            "refresh_token",
            "access_token",
            "password_hash",
        }

        for model in (AdminMatchParticipant, AdminMatchSummary, AdminMatchDetail):
            assert not set(model.model_fields) & forbidden, model.__name__

        # And no move log on the detail: replay is a separate, more
        # expensive read and folding it in would replay a game on every
        # detail open.
        assert "moves" not in AdminMatchDetail.model_fields

    @pytest.mark.asyncio
    async def test_detail_answers_404_for_a_match_that_does_not_exist(self) -> None:
        matches = InMemoryMatches([])
        with pytest.raises(HTTPException) as missing:
            await read_match(
                generate_uuid7(),
                _Identity(),  # type: ignore[arg-type]
                matches,
                InMemoryAccounts({}),  # type: ignore[arg-type]
                Headers(),  # type: ignore[arg-type]
            )
        assert missing.value.status_code == 404


class TestTheGuardIsOnEveryRoute:
    def test_no_matches_route_is_reachable_without_the_admin_dependency(self) -> None:
        """§4 and §22 — the reachability proof, asserted against the route
        table rather than by reading the source."""
        from app.modules.admin.presentation.dependencies import require_admin

        assert admin_matches_router.routes
        for route in admin_matches_router.routes:
            dependant = getattr(route, "dependant", None)
            assert dependant is not None
            assert require_admin in {sub.call for sub in dependant.dependencies}, getattr(
                route, "path", route
            )
