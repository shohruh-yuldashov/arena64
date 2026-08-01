"""The `statistics` domain and service — no database, no HTTP.

`tests/contract/test_statistics_api.py` covers the whole stack end to end.
This file covers the parts that are cheaper to assert without one and the
two that a database test cannot reach at all: the arithmetic invariants
that guard against a broken rebuild, and the cross-context constant that
has no single owner yet.
"""

from uuid import UUID, uuid4

import pytest

from app.modules.profiles.domain.ratings import STARTING_RATING
from app.modules.statistics.application.services import StatisticsService
from app.modules.statistics.domain.statistics import (
    DEFAULT_RATING,
    NO_MATCHES_PLAYED,
    PlayerStatistics,
)


class FakeStatisticsRepository:
    """One record, or none. Enough for the only decision the service makes."""

    def __init__(self, stored: PlayerStatistics | None = None) -> None:
        self._stored = stored
        self.calls: list[UUID] = []

    async def get_for_player(self, player_id: UUID) -> PlayerStatistics | None:
        self.calls.append(player_id)
        return self._stored


class TestInvariants:
    """A projection has no *business* invariant (DM-03) and does have an
    arithmetic one. These are what stand between a broken rebuild and a win
    rate above 100% on every screen that renders it."""

    def test_the_counts_must_sum_exactly(self) -> None:
        with pytest.raises(ValueError, match="must equal games_played"):
            PlayerStatistics(games_played=5, wins=1, losses=1, draws=1)

    def test_a_total_that_merely_fits_is_still_rejected(self) -> None:
        """`!=` rather than `<=`, deliberately: a total that leaves room
        would let a lost result go unnoticed, and the parts of a completed
        match record are exhaustive."""
        with pytest.raises(ValueError):
            PlayerStatistics(games_played=10, wins=2, losses=2, draws=2)

    def test_counts_cannot_be_negative(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            PlayerStatistics(games_played=-1, wins=-1)

    def test_the_peak_cannot_be_below_the_current_rating(self) -> None:
        """A peak below the present value is a projection that missed an
        update, not a rounding difference — and a profile quietly claiming
        a player has never been as good as they are now is worse than a
        loud failure."""
        with pytest.raises(ValueError, match="cannot be below"):
            PlayerStatistics(current_rating=1600, highest_rating=1500)

    def test_the_best_streak_cannot_be_below_the_active_one(self) -> None:
        with pytest.raises(ValueError, match="active win streak"):
            PlayerStatistics(best_win_streak=2, current_streak=5)

    def test_a_losing_streak_does_not_constrain_the_best_win_streak(self) -> None:
        """The streak is signed, so a losing run says nothing about the
        best winning one — `GREATEST(current_streak, 0)`, in SQL terms."""
        record = PlayerStatistics(current_streak=-7, best_win_streak=0)

        assert record.current_streak == -7


class TestWinRate:
    @pytest.mark.parametrize(
        ("wins", "losses", "draws", "expected"),
        [
            (0, 0, 0, 0.0),
            (1, 0, 0, 1.0),
            (0, 1, 0, 0.0),
            (40, 20, 40, 0.4),
            (1, 2, 0, 0.3333),
        ],
    )
    def test_it_is_the_proportion_of_games_won(
        self, wins: int, losses: int, draws: int, expected: float
    ) -> None:
        """Draws are in the denominator: this is the proportion of games
        *won*, not chess's score percentage. 40/40/20 is 0.4 here and would
        be 0.6 as a score — the divergence that made the definition worth
        recording rather than assuming."""
        record = PlayerStatistics(
            games_played=wins + losses + draws, wins=wins, losses=losses, draws=draws
        )

        assert record.win_rate == expected

    def test_zero_games_is_zero_and_not_an_error(self) -> None:
        """The division by zero this would otherwise be is the whole reason
        it is derived rather than stored."""
        assert NO_MATCHES_PLAYED.win_rate == 0.0


class TestTheEmptyRecord:
    def test_every_count_is_zero_and_both_ratings_are_the_starting_value(self) -> None:
        assert (
            PlayerStatistics(
                games_played=0,
                wins=0,
                losses=0,
                draws=0,
                current_rating=DEFAULT_RATING,
                highest_rating=DEFAULT_RATING,
                current_streak=0,
                best_win_streak=0,
            )
            == NO_MATCHES_PLAYED
        )

    def test_it_is_immutable_and_therefore_safe_to_share(self) -> None:
        """`NoMatchesStatisticsProvider` hands the same instance to every
        caller, which is only safe because nobody can mutate it."""
        with pytest.raises(AttributeError):
            NO_MATCHES_PLAYED.wins = 1  # type: ignore[misc]


class TestTheStartingRatingIsOneNumber:
    def test_the_two_contexts_agree(self) -> None:
        """`statistics.DEFAULT_RATING` and `profiles.STARTING_RATING` are
        two constants because `statistics` may not import `profiles` — the
        dependency runs the other way.

        This test is the pin. It exists to fail loudly if one is changed
        without the other, because the symptom otherwise is a profile
        reporting `statistics.current_rating: 1500` beside
        `ratings.blitz.rating: 1600` for a player who has never played, and
        nobody would know which was wrong.

        It resolves when a `rating` module owns both and one becomes a
        projection of the other — see `DEFAULT_RATING`.
        """
        assert DEFAULT_RATING == STARTING_RATING


class TestServiceAbsence:
    async def test_a_player_with_no_row_gets_the_empty_record(self) -> None:
        """Absence is a value, not a failure: a projection is built by
        folding results in, so an account that has played nothing has
        nothing to fold."""
        service = StatisticsService(FakeStatisticsRepository(stored=None))

        assert await service.for_player(uuid4()) is NO_MATCHES_PLAYED

    async def test_an_unknown_player_is_indistinguishable_from_a_new_one(self) -> None:
        """This context does not own the player directory and has no way to
        tell the two apart — which is correct, and incidentally denies an
        existence oracle to anyone probing ids."""
        service = StatisticsService(FakeStatisticsRepository(stored=None))

        assert await service.for_player(uuid4()) == await service.for_player(uuid4())

    async def test_a_stored_record_is_returned_unchanged(self) -> None:
        stored = PlayerStatistics(
            games_played=3, wins=2, losses=1, current_rating=1550, highest_rating=1600
        )
        service = StatisticsService(FakeStatisticsRepository(stored=stored))

        assert await service.for_player(uuid4()) is stored

    async def test_the_player_id_reaches_the_repository(self) -> None:
        repository = FakeStatisticsRepository()
        service = StatisticsService(repository)
        player_id = uuid4()

        await service.for_player(player_id)

        assert repository.calls == [player_id]
