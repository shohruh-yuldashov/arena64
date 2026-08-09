"""Ratings and the ladder over HTTP — A64-020.0A.

Through the real v1 router, a real `CurrentUser` and real PostgreSQL,
because what is new here is not the reading — `SqlAlchemyLeaderboardReader`
has had contract coverage since A64-017.4 (`test_leaderboard.py`) — but that
something *reaches* it. Every property asserted below is one a client
depends on and a service test cannot see: the route table, the query
parameter conversion, the envelope, the opaque cursor, and which absences
are a `404` and which are a normal answer.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.public import ProductVariant
from app.modules.rating.domain.glicko2 import Glicko2Rating, MatchOutcomeScore
from app.modules.rating.domain.keys import RatingKey, SpeedClass
from app.modules.rating.domain.player_rating import PROVISIONAL_GAMES_THRESHOLD, PlayerRating
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyPlayerRatingRepository,
)
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register as register_account

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
KEY = RatingKey(variant=ProductVariant.RUSSIAN_8X8, speed_class=SpeedClass.CLASSICAL)

LADDER_URL = "/api/v1/leaderboard"
QUERY = {"variant": KEY.variant.value, "speed_class": KEY.speed_class.value}


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


def _player(suffix: int) -> UUID:
    """A deterministic id, so a tie-break assertion is not a coin toss."""
    return UUID(f"00000000-0000-0000-0000-{suffix:012d}")


async def _store(
    session: AsyncSession,
    player_id: UUID,
    *,
    rating: float,
    games: int = 40,
    key: RatingKey = KEY,
) -> None:
    """One player on one ladder, written the way the rating consumer does."""
    await SqlAlchemyPlayerRatingRepository(session).save(
        *PlayerRating(
            player_id=player_id,
            key=key,
            rating=Glicko2Rating(rating, 100.0, 0.06),
            games_played=games,
            last_rated_at=NOW,
        ).applied(
            opponent=Glicko2Rating(rating, 100.0, 0.06),
            score=MatchOutcomeScore.draw(),
            match_id=_player(900_000 + games + int(rating)),
            at=NOW,
        )
    )


class TestPlayerRatings:
    async def test_it_answers_with_every_speed_class_played_or_not(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§1 — one request, every key, in the enum's own order.

        A speed class the caller has never played is **present** and marked
        provisional with zero games rather than omitted, because
        `RatingSnapshot.unrated()` is what `rating` answers for an absent
        row. Omitting them would push "has this player played blitz?" onto
        every client, and a client getting it wrong would render a missing
        rating as a rating of zero.

        `volatility` is deliberately absent: it is an input to the next
        calculation, not a fact about the player.
        """
        player = await register_account(client, contract_session)
        await _store(contract_session, player.id, rating=1750.0)

        response = await client.get("/api/v1/ratings/me", headers=player.auth)

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["player_id"] == str(player.id)
        assert [entry["speed_class"] for entry in body["ratings"]] == [
            speed.value for speed in SpeedClass
        ]
        assert "volatility" not in body["ratings"][0]

        played = next(e for e in body["ratings"] if e["speed_class"] == KEY.speed_class.value)
        assert played["games_played"] == 41
        assert played["is_provisional"] is False

        untouched = next(e for e in body["ratings"] if e["speed_class"] != KEY.speed_class.value)
        assert untouched["games_played"] == 0
        assert untouched["is_provisional"] is True

    async def test_an_unknown_player_is_answered_rather_than_denied(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§2 — the endpoint is not an account-existence oracle.

        `rating` answers every id with a snapshot, so "no such account" and
        "never played" are indistinguishable here **by design**: a `404` on
        one and a `200` on the other would let anybody enumerate which ids
        are real. Whether a player exists is `users`' question, behind
        `users`' own rules.
        """
        viewer = await register_account(client, contract_session)
        stranger = uuid4()

        response = await client.get(f"/api/v1/players/{stranger}/ratings", headers=viewer.auth)

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["player_id"] == str(stranger)
        assert len(body["ratings"]) == len(SpeedClass)
        assert all(entry["games_played"] == 0 for entry in body["ratings"])
        assert all(entry["is_provisional"] is True for entry in body["ratings"])


class TestLadder:
    async def test_it_is_ordered_best_first_and_shows_provisional_players(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§4, §6 — the published order, and who is on it.

        The newcomer is at the top and marked, not hidden: SPEC-RATING §6
        forbids both hiding provisional players and imposing a
        minimum-games threshold. A ladder that hid its newcomers is one
        nobody new can see themselves on.
        """
        viewer = await register_account(client, contract_session)
        await _store(contract_session, _player(2), rating=1600.0)
        await _store(contract_session, _player(3), rating=1500.0)
        await _store(
            contract_session, _player(1), rating=2100.0, games=PROVISIONAL_GAMES_THRESHOLD - 15
        )

        response = await client.get(LADDER_URL, params=QUERY, headers=viewer.auth)

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["speed_class"] == KEY.speed_class.value
        assert [entry["player_id"] for entry in body["entries"]] == [
            str(_player(index)) for index in (1, 2, 3)
        ]
        assert body["entries"][0]["is_provisional"] is True
        assert body["next_cursor"] is None

    async def test_a_cursor_walks_the_ladder_without_repeating_or_skipping(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§5 — keyset pagination, driven the way a client drives it.

        The cursor is taken from the response and sent back unread, so this
        asserts the encoding round-trips as well as the predicate. Every
        player appears exactly once across the walk and the final page
        reports no further cursor — the two failures `OFFSET` produces on a
        ladder that moves between requests.
        """
        viewer = await register_account(client, contract_session)
        for index, rating in enumerate((2000.0, 1900.0, 1800.0, 1700.0, 1600.0), start=1):
            await _store(contract_session, _player(index), rating=rating)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(3):
            page = await client.get(
                LADDER_URL,
                params={**QUERY, "limit": 2, **({"after": cursor} if cursor else {})},
                headers=viewer.auth,
            )
            assert page.status_code == 200, page.text
            body = page.json()["data"]
            seen.extend(entry["player_id"] for entry in body["entries"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        assert seen == [str(_player(index)) for index in (1, 2, 3, 4, 5)]
        assert cursor is None

    async def test_a_forged_cursor_is_refused_without_describing_the_encoding(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§9 — one error for every way a cursor can be wrong.

        A caller can do nothing differently for bad base64 than for an
        unparseable id — the answer is always "ask for the first page" — and
        distinguishing them would narrate the encoding to whoever is probing
        it. The message carries no class name, no stack and no SQL.
        """
        viewer = await register_account(client, contract_session)

        response = await client.get(
            LADDER_URL, params={**QUERY, "after": "not-a-cursor"}, headers=viewer.auth
        )

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "invalid_cursor"
        assert "Traceback" not in response.text
        assert "LeaderboardCursor" not in response.text

    async def test_the_ladder_is_not_readable_without_authentication(
        self, client: AsyncClient
    ) -> None:
        """A rating is public to *every player*, which is not the same as
        public to the internet. Every route outside `/health` is
        authenticated, and this one is no exception."""
        response = await client.get(LADDER_URL, params=QUERY)

        assert response.status_code == 401, response.text


class TestNeighbourhood:
    async def test_it_reports_a_rank_and_the_rows_on_either_side(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§7 — the "where am I?" read a client cannot compute by paging.

        `above` is nearest-last and `below` nearest-first, so a caller
        renders the three lists in sequence and gets the ladder's own order
        with the player in the middle. `span` bounds each side, so a player
        in the middle of a ladder of any size costs the same.
        """
        viewer = await register_account(client, contract_session)
        for index, rating in enumerate((2000.0, 1900.0, 1800.0, 1700.0, 1600.0), start=1):
            await _store(contract_session, _player(index), rating=rating)

        response = await client.get(
            f"{LADDER_URL}/around/{_player(3)}",
            params={**QUERY, "span": 1},
            headers=viewer.auth,
        )

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["rank"] == 3
        assert body["entry"]["player_id"] == str(_player(3))
        assert [entry["player_id"] for entry in body["above"]] == [str(_player(2))]
        assert [entry["player_id"] for entry in body["below"]] == [str(_player(4))]

    async def test_a_player_with_no_rating_in_that_key_is_not_on_the_ladder(
        self, contract_session: AsyncSession, client: AsyncClient
    ) -> None:
        """§7 — and the one place a `404` is right.

        Deliberately unlike `/players/{id}/ratings`, which answers every id:
        a rating exists for everybody, a *ranking* only for a player with a
        stored row. Asserted with a player who is rated in another speed
        class, so the absence is the key's and not the platform's.
        """
        viewer = await register_account(client, contract_session)
        await _store(
            contract_session,
            _player(1),
            rating=1800.0,
            key=RatingKey(variant=KEY.variant, speed_class=SpeedClass.BLITZ),
        )

        response = await client.get(
            f"{LADDER_URL}/around/{_player(1)}", params=QUERY, headers=viewer.auth
        )

        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
