"""Match history and replay, end to end — SPEC-REPLAY §1, §4, §7.

Against real PostgreSQL, and driven **through the composition root** rather
than by constructing the services directly. That is deliberate: A64-018's
reachability requirement says a component that is implemented but unwired is
incomplete, and a test that builds its own object graph proves the classes
work while proving nothing about whether anything can reach them.

So every test here resolves its reader the way a request would —
`get_match_history(session)` / `get_match_replay(session)` — which means the
factory, the repository it names, the service it wraps and the port it
returns are all on the asserted path.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.engine import PlayerSide
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.infrastructure.models import MatchRecordModel
from app.modules.game.presentation.dependencies import get_match_history, get_match_replay
from app.modules.game.public import UnsupportedEngineVersion

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _id(suffix: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{suffix:012d}")


async def _finished(
    session: AsyncSession,
    *,
    match_id: UUID,
    light: UUID,
    dark: UUID,
    created_at: datetime,
    rated: bool = True,
    engine_version: int = 2,
) -> None:
    session.add(
        MatchRecordModel(
            id=match_id,
            pairing_id=_id(500_000 + int(str(match_id)[-6:])),
            variant=ProductVariant.RUSSIAN_8X8,
            rated=rated,
            engine_version=engine_version,
            status=MatchRecordStatus.COMPLETED,
            light_player_id=light,
            dark_player_id=dark,
            # Derived from the match: `uq_match__light_ticket` and
            # `uq_match__dark_ticket` make a ticket produce at most one
            # match, so a shared constant would collide on the second row.
            light_ticket_id=_id(600_000 + int(str(match_id)[-6:]) * 2),
            dark_ticket_id=_id(600_001 + int(str(match_id)[-6:]) * 2),
            acceptance_deadline=created_at + timedelta(seconds=30),
            created_at=created_at,
            settled_at=created_at + timedelta(minutes=5),
            ended_at=created_at + timedelta(minutes=5),
            outcome=MatchOutcome.WIN,
            termination_reason=TerminationReason.RESIGNATION,
            winner=PlayerSide.LIGHT,
            ply_number=24,
        )
    )
    await session.flush()


class TestHistoryIsReachableAndOrdered:
    async def test_a_players_finished_matches_page_newest_first(
        self, contract_session: AsyncSession
    ) -> None:
        """§1 and §7, through the composition root.

        `get_match_history` is the factory a route resolves, so this
        exercises the whole path — factory, `SqlAlchemyMatchHistoryRepository`,
        `GameMatchHistory`, and the published `MatchHistoryReader`. A test
        that constructed the repository itself would leave the factory
        unproven, which is exactly the gap two audits found.

        Newest first, tie-broken by id, so the order is total: a page cannot
        skip or repeat a match when another game finishes between reads.
        """
        player, opponent = _id(1), _id(2)
        for index in range(3):
            await _finished(
                contract_session,
                match_id=_id(100 + index),
                light=player,
                dark=opponent,
                created_at=NOW - timedelta(days=index),
            )

        history = get_match_history(contract_session)
        page = await history.history_for(player, limit=2)

        assert [entry.match_id for entry in page.entries] == [_id(100), _id(101)]
        assert page.next_cursor is not None

        rest = await history.history_for(player, after=page.next_cursor, limit=2)
        assert [entry.match_id for entry in rest.entries] == [_id(102)]
        assert rest.next_cursor is None

    async def test_history_carries_what_a_visibility_check_needs(
        self, contract_session: AsyncSession
    ) -> None:
        """§3 and §4 — the two fields the caller decides on.

        `rated` is what SPEC-REPLAY §3's privacy rule branches on, and
        `engine_version` is what tells a client a replay will be refused
        *before* it asks. Both are stored facts, which is what lets §4 keep
        an unreplayable match visible: nothing about this entry touches the
        engine.
        """
        player = _id(1)
        await _finished(
            contract_session,
            match_id=_id(200),
            light=player,
            dark=_id(2),
            created_at=NOW,
            rated=False,
            engine_version=1,
        )

        entry = await get_match_history(contract_session).entry_for(_id(200))

        assert entry is not None
        assert entry.rated is False
        assert entry.engine_version == 1
        assert entry.winner is PlayerSide.LIGHT
        assert entry.ply_number == 24


class TestReplayIsReachableAndRefusesOldRules:
    async def test_an_unsupported_engine_version_is_refused_not_approximated(
        self, contract_session: AsyncSession
    ) -> None:
        """§4, through the composition root.

        `SUPPORTED_ENGINE_VERSIONS` holds version 2. A match recorded under
        version 1 keeps its history — asserted above — and its replay
        **raises** rather than reconstructing the game under rules it was
        not played under. A64-014.8's argument is that the reconstruction
        could end differently from the game that was actually rated and
        displayed, which would make the archive disagree with history.

        The exception is the **published** `UnsupportedEngineVersion`, not
        the domain one, so a consumer branches on a `game.public` type — the
        boundary §6 is about.
        """
        await _finished(
            contract_session,
            match_id=_id(300),
            light=_id(1),
            dark=_id(2),
            created_at=NOW,
            engine_version=1,
        )

        with pytest.raises(UnsupportedEngineVersion):
            await get_match_replay(contract_session).replay_of(_id(300))

    async def test_an_unknown_match_replays_as_none_rather_than_raising(
        self, contract_session: AsyncSession
    ) -> None:
        """`None` and the refusal are different answers, deliberately.

        One means "no such game"; the other means "this game exists and you
        may not see it reconstructed". Collapsing them would make a client
        unable to tell a typo from a rules gap — and would let a caller
        probe for match ids by watching which error came back.
        """
        assert await get_match_replay(contract_session).replay_of(_id(999)) is None
