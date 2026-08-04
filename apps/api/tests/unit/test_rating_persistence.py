"""The boundaries A64-017.2 introduced — SPEC-RATING §7.6, §14.

Not the arithmetic (`test_glicko2.py`) and not the aggregate
(`test_player_rating.py`). What is new here is that a rating **crosses
modules** correctly: `matchmaking` reads it through `rating.public`, `game`
stores it and computes nothing, and `profiles` keeps a shipped API contract
while speaking the new vocabulary underneath.

The unique constraint that makes PR-1 true is asserted against real
PostgreSQL in `tests/contract/`, because an in-memory double cannot refuse
a concurrent insert — which is the whole property.
"""

from uuid import uuid4

import pytest

from app.modules.game.domain.match_record import SeatRating
from app.modules.game.public import ProductVariant
from app.modules.profiles.domain.ratings import RatingCategory
from app.modules.profiles.infrastructure.rating_compatibility import PublishedRatingProvider
from app.modules.rating.public import RatingKey, RatingSnapshot, SpeedClass


class _StubReader:
    """A `RatingReader` a test dictates the answers of."""

    def __init__(self, **by_player: RatingSnapshot) -> None:
        self.keys: list[RatingKey] = []
        self._answers = {uuid4(): snapshot for snapshot in by_player.values()}
        self.answer = next(iter(self._answers.values()), RatingSnapshot.unrated())

    async def rating_for(self, player_id, *, key: RatingKey) -> RatingSnapshot:  # type: ignore[no-untyped-def]
        self.keys.append(key)
        return self.answer

    async def ratings_for(self, player_ids, *, key: RatingKey):  # type: ignore[no-untyped-def]
        self.keys.append(key)
        return dict.fromkeys(player_ids, self.answer)

    async def ratings_across(self, player_ids, *, keys):  # type: ignore[no-untyped-def]
        self.keys.extend(keys)
        return {(player_id, key): self.answer for player_id in player_ids for key in keys}


class TestTheSeatSnapshot:
    def test_it_carries_everything_a_later_calculation_needs(self) -> None:
        """PR-3, stated as the contents of one value.

        The calculation runs months after the game, from this and nothing
        else — so a field missing here is a rating that cannot be computed
        or cannot be explained. The deviation and volatility are the two
        that a "just store the rating" shortcut would drop, and dropping
        them makes PR-3 unimplementable rather than merely lossy.
        """
        snapshot = SeatRating(
            value=1487.5,
            deviation=118.2,
            volatility=0.0592,
            games_played=31,
            is_provisional=False,
            speed_class=SpeedClass.CLASSICAL.value,
        )

        assert (snapshot.value, snapshot.deviation, snapshot.volatility) == (
            1487.5,
            118.2,
            0.0592,
        )
        assert snapshot.games_played == 31
        assert snapshot.is_provisional is False
        assert snapshot.speed_class == "classical"


class TestTheProfileCompatibilityMapping:
    @pytest.mark.asyncio
    async def test_the_shipped_response_shape_survives_the_new_vocabulary(self) -> None:
        """AC-7, and the reason `RatingCategory` was not simply renamed.

        `GET /profiles/{handle}` has shipped `ratings.{classic, rapid,
        blitz}` since A64-012.1. The three fields are still there and still
        populated — but every key the adapter *reads* is a `SpeedClass`, so
        nothing below presentation knows the old spelling exists.

        `classic` maps to `CLASSICAL`, which is the whole of the alias: one
        translation, at one boundary, with a removal date.
        """
        reader = _StubReader(
            only=RatingSnapshot(
                value=1623.4,
                deviation=90.0,
                volatility=0.06,
                games_played=40,
                is_provisional=False,
            )
        )

        ratings = await PublishedRatingProvider(reader).ratings_for(uuid4())

        assert {snapshot.rating for snapshot in ratings.as_map().values()} == {1623}
        assert ratings.classic.is_provisional is False
        assert ratings.classic.games_played == 40

        # Every read went to a `SpeedClass`, and `classic` became
        # `CLASSICAL` rather than travelling as itself.
        assert {key.speed_class for key in reader.keys} == {
            SpeedClass.CLASSICAL,
            SpeedClass.RAPID,
            SpeedClass.BLITZ,
        }
        assert all(key.variant is ProductVariant.RUSSIAN_8X8 for key in reader.keys)
        assert set(RatingCategory) == {
            RatingCategory.CLASSIC,
            RatingCategory.RAPID,
            RatingCategory.BLITZ,
        }

    @pytest.mark.asyncio
    async def test_a_player_with_no_rating_row_reads_as_unrated_rather_than_missing(
        self,
    ) -> None:
        """§7.5's two-sided state, at the boundary that renders it.

        A player who has never played has **no row**, and the reader answers
        with the starting triple rather than `None`. That equivalence is
        what keeps "has this player ever played" out of every consumer — a
        profile that had to branch on absence would eventually render a
        blank rating instead of an honest 1500.
        """
        ratings = await PublishedRatingProvider(_StubReader()).ratings_for(uuid4())

        assert ratings.classic.rating == 1500
        assert ratings.classic.games_played == 0
        assert ratings.classic.is_provisional is True
