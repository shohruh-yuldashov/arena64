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
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.engine import PlayerSide
from app.modules.game.domain.match_record import MatchRecordStatus
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.infrastructure.models import MatchRecordModel, MoveLogModel
from app.modules.game.presentation.dependencies import get_match_history, get_match_replay
from app.modules.game.public import UnsupportedEngineVersion
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):
    """The real API, over this suite's session.

    The whole point of §7: a route file that exists without router
    registration is incomplete, and only a request that reaches it proves
    otherwise.
    """
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


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


class TestTheApiIsReachable:
    """§7 — the route, the dependency and the mapper, through the real app.

    Driven with the API client rather than by calling the handler: a route
    file that exists without router registration is incomplete, and only a
    request that reaches it proves otherwise. These four also cover §9's
    visibility cases end to end.
    """

    async def test_a_rated_match_is_public_and_a_casual_one_is_not(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§3, from a stranger's side.

        The rated match appears; the casual one is **absent** rather than
        redacted, because a placeholder would confirm it exists.
        """
        stranger = await register(client)
        owner = _id(700)
        await _finished(
            contract_session,
            match_id=_id(400),
            light=owner,
            dark=_id(9),
            created_at=NOW,
            rated=True,
        )
        await _finished(
            contract_session,
            match_id=_id(401),
            light=owner,
            dark=_id(9),
            created_at=NOW - timedelta(hours=1),
            rated=False,
        )
        await contract_session.commit()

        response = await client.get(f"/api/v1/players/{owner}/matches", headers=stranger.auth)

        assert response.status_code == 200, response.text
        seen = {entry["match_id"] for entry in response.json()["data"]["entries"]}
        assert seen == {str(_id(400))}

    async def test_a_participant_sees_their_own_casual_match_and_the_opponent(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§3 and §4 — the same casual match, read by somebody who played it.

        `opponent_id` is populated only for a participant: it makes a
        personal history readable and is meaningless when a stranger reads
        somebody else's record.
        """
        player = await register(client)
        await _finished(
            contract_session,
            match_id=_id(410),
            light=player.id,
            dark=_id(9),
            created_at=NOW,
            rated=False,
        )
        await contract_session.commit()

        response = await client.get(f"/api/v1/players/{player.id}/matches", headers=player.auth)

        entries = response.json()["data"]["entries"]
        assert [entry["match_id"] for entry in entries] == [str(_id(410))]
        assert entries[0]["opponent_id"] == str(_id(9))
        assert entries[0]["rated"] is False

    async def test_a_hidden_match_and_an_unknown_match_are_indistinguishable(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§3 and §8 — the assertion the privacy rule rests on.

        A casual match the viewer did not play and an id that was never
        issued produce the **same** status and the same code. A `403` on the
        first would confirm it exists, which is enough to enumerate match
        ids and learn who plays casually with whom.
        """
        stranger = await register(client)
        await _finished(
            contract_session,
            match_id=_id(420),
            light=_id(8),
            dark=_id(9),
            created_at=NOW,
            rated=False,
        )
        await contract_session.commit()

        hidden = await client.get(f"/api/v1/matches/{_id(420)}/replay", headers=stranger.auth)
        unknown = await client.get(f"/api/v1/matches/{_id(999)}/replay", headers=stranger.auth)

        assert hidden.status_code == unknown.status_code == 404
        assert hidden.json()["code"] == unknown.json()["code"] == "not_found"

    async def test_an_unsupported_version_returns_its_own_stable_code(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§4 and §8 — refused, specifically, and only to somebody entitled.

        The viewer played this match, so they could already see it in their
        history; the refusal therefore discloses nothing and is allowed to
        be specific. A client shows the game and hides the replay control,
        which a bare `conflict` could not tell it to do.
        """
        player = await register(client)
        await _finished(
            contract_session,
            match_id=_id(430),
            light=player.id,
            dark=_id(9),
            created_at=NOW,
            engine_version=1,
        )
        await contract_session.commit()

        response = await client.get(f"/api/v1/matches/{_id(430)}/replay", headers=player.auth)

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "unsupported_engine_version"


class TestTheAuditScenarios:
    """A64-018.4 §1, §5, §6 — the whole flow, and the two behaviours that
    only appear when the pieces are put together."""

    async def test_an_unsupported_version_is_refused_without_reading_the_move_log(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§3 and SPEC-REPLAY §4 — "no attempt is made", audited.

        A64-018.4 found that `PersistedMatchReplay.replay_data` loads every
        ply *before* `ReplayEngine` examines the version, so an unsupported
        match cost a full log read for an answer that one row already had.
        The refusal moved to `VisibleMatchReplay`, which is holding the
        match entry anyway.

        Proven by giving the match a move log whose rows would **fail** a
        replay — the position hashes are nonsense — and asserting the API
        still answers `unsupported_engine_version`. If the log were read and
        replayed, this would surface as a hash mismatch instead.
        """
        player = await register(client)
        match_id = _id(500)
        await _finished(
            contract_session,
            match_id=match_id,
            light=player.id,
            dark=_id(9),
            created_at=NOW,
            engine_version=1,
        )
        contract_session.add(
            MoveLogModel(
                id=_id(700_001),
                match_id=match_id,
                ply_number=1,
                seat="light",
                path=["c3", "d4"],
                captured=[],
                promoted_to=None,
                position_hash="not-a-real-fingerprint",
                engine_version=1,
                created_at=NOW,
            )
        )
        await contract_session.commit()

        response = await client.get(f"/api/v1/matches/{match_id}/replay", headers=player.auth)

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "unsupported_engine_version"

        # …and the match is still listed, which is §4's whole point: the
        # metadata survives, only the reconstruction is refused.
        listed = await client.get(f"/api/v1/players/{player.id}/matches", headers=player.auth)
        assert [e["match_id"] for e in listed.json()["data"]["entries"]] == [str(match_id)]

    async def test_visibility_filtering_makes_pages_sparse_without_losing_entries(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§5 — the documented consequence of filtering after the query.

        A stranger paging a player whose record is mostly casual sees
        **short pages**: the query returns `limit` rows and the filter
        removes the hidden ones, so a page of two can come back with one
        entry — or none — while `next_cursor` is still set.

        That is the accepted behaviour, not a defect, and it is asserted
        rather than left to be discovered: the alternative is filtering
        inside the query, which SPEC-REPLAY §3 rejects because the cursor
        would then come from a row the caller cannot see.

        **Nothing is lost or repeated.** Walking every page yields each
        visible match exactly once, which is the property that actually
        matters.
        """
        stranger = await register(client)
        owner = _id(800)
        for index in range(6):
            await _finished(
                contract_session,
                match_id=_id(600 + index),
                light=owner,
                dark=_id(9),
                created_at=NOW - timedelta(hours=index),
                rated=index % 3 == 0,
            )
        await contract_session.commit()

        seen: list[str] = []
        cursor: str | None = None
        pages = 0

        while pages < 6:
            url = f"/api/v1/players/{owner}/matches?limit=2"
            response = await client.get(
                url + (f"&after={cursor}" if cursor else ""), headers=stranger.auth
            )
            body = response.json()["data"]
            seen.extend(entry["match_id"] for entry in body["entries"])
            cursor = body["next_cursor"]
            pages += 1
            if cursor is None:
                break

        # Two of six are rated, newest first, each exactly once.
        assert seen == [str(_id(600)), str(_id(603))]
        assert cursor is None
