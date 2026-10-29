"""`GET /leaderboard/players` against real accounts and ratings — A64-033.

The composition `tests/unit/test_leaderboard_service.py` proves with fakes,
here through the real router, real `users` and real `rating` rows, so the
privacy rule rests on the read that actually enforces it: a country set
through `PATCH /profile` and hidden through `PATCH /profile/privacy`.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.public import ProductVariant
from app.modules.rating.domain.glicko2 import Glicko2Rating, MatchOutcomeScore
from app.modules.rating.domain.keys import RatingKey, SpeedClass
from app.modules.rating.domain.player_rating import PlayerRating
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyPlayerRatingRepository,
)
from tests.contract.contract_app import build_contract_app, contract_client
from tests.contract.test_matchmaking_queue_api import register

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
BLITZ = RatingKey(variant=ProductVariant.RUSSIAN_8X8, speed_class=SpeedClass.BLITZ)
URL = "/api/v1/leaderboard/players"


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def _rate(session: AsyncSession, player_id: UUID, rating: float, match: int) -> None:
    await SqlAlchemyPlayerRatingRepository(session).save(
        *PlayerRating(
            player_id=player_id,
            key=BLITZ,
            rating=Glicko2Rating(rating, 60.0, 0.06),
            games_played=40,
            last_rated_at=NOW,
        ).applied(
            opponent=Glicko2Rating(rating, 60.0, 0.06),
            score=MatchOutcomeScore.draw(),
            match_id=UUID(f"00000000-0000-4000-9000-{match:012d}"),
            at=NOW,
        )
    )


class TestLeaderboardPlayers:
    async def test_a_country_ladder_never_reveals_a_hidden_country(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        shown = await register(client, contract_session)
        hidden = await register(client, contract_session)
        elsewhere = await register(client, contract_session)
        for player, country in ((shown, "UZ"), (hidden, "UZ"), (elsewhere, "KZ")):
            updated = await client.patch(
                "/api/v1/profile", headers=player.auth, json={"country": country}
            )
            assert updated.status_code == 200, updated.text
        hid = await client.patch(
            "/api/v1/profile/privacy", headers=hidden.auth, json={"show_country": False}
        )
        assert hid.status_code == 200, hid.text

        await _rate(contract_session, hidden.id, 1900.0, 1)
        await _rate(contract_session, shown.id, 1800.0, 2)
        await _rate(contract_session, elsewhere.id, 1700.0, 3)
        await contract_session.commit()

        uz = await client.get(
            URL, params={"speed_class": "blitz", "country": "UZ"}, headers=shown.auth
        )
        everyone = await client.get(URL, params={"speed_class": "blitz"}, headers=shown.auth)

        assert uz.status_code == 200, uz.text
        assert [(p["rank"], p["player_id"]) for p in uz.json()["data"]["players"]] == [
            (1, str(shown.id))
        ]
        global_rows = everyone.json()["data"]["players"]
        assert [p["player_id"] for p in global_rows] == [
            str(hidden.id),
            str(shown.id),
            str(elsewhere.id),
        ]
        # On the global ladder the hidden country stays hidden.
        assert global_rows[0]["country"] is None

    async def test_your_own_place_says_why_you_are_not_on_a_country_ladder(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        player = await register(client, contract_session)
        await _rate(contract_session, player.id, 1600.0, 10)
        await contract_session.commit()

        global_place = await client.get(
            f"{URL}/me", params={"speed_class": "blitz"}, headers=player.auth
        )
        country_place = await client.get(
            f"{URL}/me", params={"speed_class": "blitz", "country": "UZ"}, headers=player.auth
        )
        unrated = await client.get(
            f"{URL}/me", params={"speed_class": "rapid"}, headers=player.auth
        )

        assert global_place.json()["data"]["status"] == "ranked"
        assert country_place.json()["data"]["status"] == "not_listed"
        assert unrated.json()["data"]["status"] == "unrated"
