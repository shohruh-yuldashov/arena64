"""The leaderboard, against real PostgreSQL — A64-017.4.

Against the database rather than a fake, because every property that
matters here is the database's: a total order the index actually serves, a
keyset predicate that resumes exactly where a page stopped, and the fact
that the standings *are* the rating rows rather than a copy of them.

An in-memory double would sort a Python list — which proves that
`sorted(key=...)` works, not that the query does.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.game.public import ProductVariant
from app.modules.rating.domain.glicko2 import Glicko2Rating, MatchOutcomeScore
from app.modules.rating.domain.keys import RatingKey, SpeedClass
from app.modules.rating.domain.player_rating import PROVISIONAL_GAMES_THRESHOLD, PlayerRating
from app.modules.rating.infrastructure.repositories.leaderboard_repository import (
    SqlAlchemyLeaderboardReader,
)
from app.modules.rating.infrastructure.repositories.player_rating_repository import (
    SqlAlchemyPlayerRatingRepository,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
KEY = RatingKey(variant=ProductVariant.RUSSIAN_8X8, speed_class=SpeedClass.CLASSICAL)
OTHER_KEY = RatingKey(variant=ProductVariant.RUSSIAN_8X8, speed_class=SpeedClass.BLITZ)


def _player(suffix: int) -> UUID:
    """A deterministic id, so tie-break ordering is assertable.

    `player_id` is the ordering's last resort, so a test about ties needs
    to know which id sorts first — random ones would make the assertion a
    coin toss.
    """
    return UUID(f"00000000-0000-0000-0000-{suffix:012d}")


async def _store(
    session: AsyncSession,
    player_id: UUID,
    *,
    rating: float,
    deviation: float = 100.0,
    games: int = 40,
    key: RatingKey = KEY,
) -> None:
    await SqlAlchemyPlayerRatingRepository(session).save(
        *PlayerRating(
            player_id=player_id,
            key=key,
            rating=Glicko2Rating(rating, deviation, 0.06),
            games_played=games,
            last_rated_at=NOW,
        ).applied(
            opponent=Glicko2Rating(rating, deviation, 0.06),
            score=MatchOutcomeScore.draw(),
            match_id=_player(900_000 + games + int(rating)),
            at=NOW,
        )
    )


class TestOrdering:
    async def test_it_is_total_and_never_relies_on_row_order(
        self, contract_session: AsyncSession
    ) -> None:
        """§4 — rating DESC, deviation ASC, player_id ASC.

        Built so that each key is the *only* one that separates a pair:
        two players share a rating and are split by deviation, and two
        share both and are split by id. A query missing any one of the
        three would return them in whatever order the heap happened to
        hold, which is stable enough to pass by luck on a small table and
        is not an order at all.

        The deviation rule is the one with a product argument behind it:
        between two players on the same rating, the one the platform is
        more sure about ranks higher.
        """
        # Stored deliberately out of order.
        await _store(contract_session, _player(3), rating=1600.0, deviation=50.0)
        await _store(contract_session, _player(1), rating=1800.0, deviation=200.0)
        await _store(contract_session, _player(2), rating=1600.0, deviation=200.0)
        await _store(contract_session, _player(4), rating=1600.0, deviation=50.0, games=41)

        page = await SqlAlchemyLeaderboardReader(contract_session).page(KEY)

        assert [entry.player_id for entry in page.entries] == [
            _player(1),  # highest rating
            _player(3),  # tied at 1600, tightest deviation, lower id
            _player(4),  # tied at 1600 and on deviation, higher id
            _player(2),  # tied at 1600, widest deviation
        ]

    async def test_a_key_only_shows_its_own_players(self, contract_session: AsyncSession) -> None:
        """§1 — the ladder is scoped to `(variant, speed class)`.

        A player rated in `blitz` does not appear on the `classical` board,
        even though both rows live in one relation. That is what makes the
        key a key rather than a label.
        """
        await _store(contract_session, _player(1), rating=1700.0)
        await _store(contract_session, _player(2), rating=2500.0, key=OTHER_KEY)

        page = await SqlAlchemyLeaderboardReader(contract_session).page(KEY)

        assert [entry.player_id for entry in page.entries] == [_player(1)]


class TestPagination:
    async def test_a_cursor_resumes_exactly_where_the_page_stopped(
        self, contract_session: AsyncSession
    ) -> None:
        """§5 — keyset, and the property `OFFSET` cannot give.

        Five players, two at a time. Every player appears exactly once
        across the three pages and none is skipped — which is the whole
        assertion, because a cursor that compared only the rating would
        skip the second of any tied pair, and an offset would shift the
        moment a rating moved between pages.

        Two of the five are deliberately tied on rating **and** deviation,
        so the tie-break branch of the predicate is exercised rather than
        merely present.
        """
        await _store(contract_session, _player(1), rating=2000.0)
        await _store(contract_session, _player(2), rating=1900.0)
        await _store(contract_session, _player(3), rating=1900.0)
        await _store(contract_session, _player(4), rating=1800.0)
        await _store(contract_session, _player(5), rating=1700.0)

        reader = SqlAlchemyLeaderboardReader(contract_session)
        seen: list[UUID] = []
        cursor = None

        for _ in range(3):
            page = await reader.page(KEY, after=cursor, limit=2)
            seen.extend(entry.player_id for entry in page.entries)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert seen == [_player(i) for i in (1, 2, 3, 4, 5)]
        assert cursor is None

    async def test_the_last_page_reports_no_further_cursor(
        self, contract_session: AsyncSession
    ) -> None:
        """A full page that is also the last one ends the walk.

        The count alone cannot say so — a ladder whose length is an exact
        multiple of the limit would send a reader back for an empty page
        forever. `limit + 1` is what distinguishes them.
        """
        await _store(contract_session, _player(1), rating=1600.0)
        await _store(contract_session, _player(2), rating=1500.0)

        page = await SqlAlchemyLeaderboardReader(contract_session).page(KEY, limit=2)

        assert len(page.entries) == 2
        assert page.next_cursor is None


class TestProjectionConsistency:
    async def test_a_rating_update_is_visible_immediately_and_marks_provisional(
        self, contract_session: AsyncSession
    ) -> None:
        """§2, §3 and §6 in one, because they are one property.

        The standings are the rating rows. There is no relay to wait for
        and no projection to rebuild, so a rating written in this
        transaction is on the board in the next read — asserted by moving a
        player and seeing the board reorder, with **no** event published
        and nothing else run in between.

        The provisional player is **shown** and marked, at the top, with
        three games. §6 forbids hiding them and forbids a minimum-games
        threshold: a ladder that hid its newcomers is one nobody new can
        see themselves on.
        """
        reader = SqlAlchemyLeaderboardReader(contract_session)
        repository = SqlAlchemyPlayerRatingRepository(contract_session)

        await _store(contract_session, _player(1), rating=1600.0)
        await _store(contract_session, _player(2), rating=1500.0)
        await _store(
            contract_session,
            _player(3),
            rating=2000.0,
            games=PROVISIONAL_GAMES_THRESHOLD - 23,
        )

        newcomer = (await reader.page(KEY)).entries[0]
        assert newcomer.player_id == _player(3)
        assert newcomer.is_provisional is True
        assert newcomer.games_played < PROVISIONAL_GAMES_THRESHOLD

        # Move the bottom player above the top one, in this transaction.
        loser = await repository.load(_player(2), key=KEY)
        promoted, adjustment = loser.based_on(Glicko2Rating(2400.0, 60.0, 0.06)).applied(
            opponent=Glicko2Rating(1500.0, 60.0, 0.06),
            score=MatchOutcomeScore.win(),
            match_id=_player(777_777),
            at=NOW,
        )
        await repository.save(promoted, adjustment)

        assert (await reader.page(KEY)).entries[0].player_id == _player(2)
