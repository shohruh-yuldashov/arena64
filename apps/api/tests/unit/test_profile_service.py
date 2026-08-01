"""`ProfileService` and the `profiles` domain — composition, no database.

A64-012.1 asks for essential tests only, and names three: successful
lookup, profile not found, and case-insensitive lookup. All three are here
and again in `tests/contract/test_profiles_api.py` end to end; this file is
the fast half and adds the two properties that are cheaper to assert
without HTTP.

Runs the **real** `UserService` and `PublicProfileService` over
`FakeUserRepository`, which the contract suite in
`tests/contract/test_user_repository.py` holds to the same behaviour as the
SQLAlchemy adapter. That matters for the case-insensitivity test in
particular: folding is the repository's, so a hand-rolled fake reader would
have proven only that the fake folds.

The rating and statistics providers are the real placeholders — they are
the production wiring, and substituting them would leave the composition
untested.
"""

from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

from app.core.enums import Locale
from app.modules.profiles.application.services import ProfileService
from app.modules.profiles.domain.exceptions import ProfileNotFound
from app.modules.profiles.domain.ratings import STARTING_RATING, PlayerRatings, RatingCategory
from app.modules.profiles.infrastructure import (
    NoMatchesStatisticsProvider,
    UnratedRatingProvider,
)
from app.modules.statistics.public import PlayerStatistics
from app.modules.users.application.services import UserService
from app.modules.users.application.services.public_profile_service import PublicProfileService
from app.modules.users.domain.entities import User
from app.modules.users.domain.value_objects import (
    Bio,
    CountryCode,
    DisplayName,
    Email,
    Timezone,
    Username,
)
from tests.fakes.user_repository import FakeUserRepository

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class _FixedClock:
    def now(self) -> datetime:
        return NOW


class _NullUnitOfWork:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def account(*, username: str = "Player_One", is_active: bool = True) -> User:
    user = User.create(
        username=Username(username),
        email=Email("player.one@example.com"),
        password_hash="argon2id$fake$notarealhash",
        preferred_language=Locale.EN,
        timezone=Timezone("Europe/London"),
        created_at=NOW,
    )
    user.display_name = DisplayName("Player One")
    user.bio = Bio("I play chess.")
    user.country = CountryCode("GB")
    user.is_active = is_active
    return user


@pytest.fixture
def user() -> User:
    return account()


@pytest.fixture
def service(user: User) -> ProfileService:
    users = UserService(
        users=FakeUserRepository([user]),
        unit_of_work=_NullUnitOfWork(),
        clock=_FixedClock(),
    )
    return ProfileService(
        profiles=PublicProfileService(users),
        ratings=UnratedRatingProvider(),
        statistics=NoMatchesStatisticsProvider(),
    )


class TestSuccessfulLookup:
    async def test_returns_the_players_identity(self, service: ProfileService, user: User) -> None:
        profile = await service.get_public_profile("Player_One")

        assert profile.identity.id == user.id
        assert profile.identity.username == "Player_One"
        assert profile.identity.display_name == "Player One"

    async def test_returns_the_presentational_fields(self, service: ProfileService) -> None:
        profile = await service.get_public_profile("Player_One")

        assert profile.identity.bio == "I play chess."
        assert profile.identity.country == "GB"
        assert profile.identity.created_at == NOW

    async def test_carries_no_email(self, service: ProfileService) -> None:
        """Asserted on the *type*, not on a value. The port returns
        `PublicUserProfile`, which has no email field, so this cannot be
        made to fail by a careless mapping — which is the point of the
        separate DTO. A test that checked `profile.identity.email != ...`
        would not compile."""
        profile = await service.get_public_profile("Player_One")

        assert not hasattr(profile.identity, "email")
        assert "email" not in type(profile.identity).model_fields

    async def test_last_seen_is_absent_rather_than_invented(self, service: ProfileService) -> None:
        """Presence is out of scope. Nothing stored — not `updated_at`, not
        a session's `last_used_at` — may stand in for it."""
        assert (await service.get_public_profile("Player_One")).last_seen is None


class TestProfileNotFound:
    async def test_an_unknown_username_raises(self, service: ProfileService) -> None:
        with pytest.raises(ProfileNotFound):
            await service.get_public_profile("nobody_here")

    async def test_a_deactivated_account_is_indistinguishable_from_missing(
        self, user: User
    ) -> None:
        """Publishing *which* accounts are withdrawn tells an impersonator
        whose handle is safe to adopt. Asserted as an equality between the
        two rejections rather than against a constant, because the failure
        being guarded against is a difference, whatever form it takes."""
        users = UserService(
            users=FakeUserRepository([account(is_active=False)]),
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
        )
        service = ProfileService(
            profiles=PublicProfileService(users),
            ratings=UnratedRatingProvider(),
            statistics=NoMatchesStatisticsProvider(),
        )

        with pytest.raises(ProfileNotFound) as deactivated:
            await service.get_public_profile("Player_One")
        with pytest.raises(ProfileNotFound) as missing:
            await service.get_public_profile("nobody_here")

        assert deactivated.value.message == missing.value.message
        assert deactivated.value.code == missing.value.code

    async def test_a_malformed_username_is_not_found_rather_than_an_error(
        self, service: ProfileService
    ) -> None:
        """A name that cannot be a handle cannot belong to anyone. Letting
        `InvalidUsername` escape here would tell a scraper which of its
        guesses were even *shaped* like real usernames; the route still
        rejects these earlier with a path-parameter 422, which is the right
        feedback for a typo."""
        with pytest.raises(ProfileNotFound):
            await service.get_public_profile("no spaces allowed!")


class TestCaseInsensitiveLookup:
    @pytest.mark.parametrize(
        "queried",
        ["Player_One", "player_one", "PLAYER_ONE", "PlAyEr_OnE"],
        ids=["as-registered", "lower", "upper", "mixed"],
    )
    async def test_every_casing_resolves_to_the_same_account(
        self, service: ProfileService, user: User, queried: str
    ) -> None:
        """UP-1: usernames are unique on their folded form, so lookup must
        match on it too — otherwise a handle is reachable at one casing and
        404s at another."""
        assert (await service.get_public_profile(queried)).identity.id == user.id

    async def test_the_response_preserves_the_registered_casing(
        self, service: ProfileService
    ) -> None:
        """Matching folds; rendering does not. A client shows the player
        the name they chose, not the one the visitor typed."""
        profile = await service.get_public_profile("PLAYER_ONE")

        assert profile.identity.username == "Player_One"


class TestPlaceholderRatingsAndStatistics:
    async def test_every_category_is_present(self, service: ProfileService) -> None:
        """A profile whose `ratings` object varies in shape by player is
        one every client writes defensive code against."""
        ratings = (await service.get_public_profile("Player_One")).ratings.as_map()

        assert set(ratings) == set(RatingCategory)

    async def test_every_rating_is_marked_provisional(self, service: ProfileService) -> None:
        """PR-6. A starting value published without the mark is a claim the
        platform cannot support, and it misleads an opponent deciding
        whether to accept a challenge."""
        ratings = (await service.get_public_profile("Player_One")).ratings

        assert all(snapshot.is_provisional for snapshot in ratings.as_map().values())
        assert all(snapshot.rating == STARTING_RATING for snapshot in ratings.as_map().values())

    async def test_statistics_are_zero_and_win_rate_is_not_an_error(
        self, service: ProfileService
    ) -> None:
        statistics = (await service.get_public_profile("Player_One")).statistics

        # Not `None`: this account is on the default `show_statistics=True`.
        # The assertion is part of the test rather than a type-checker
        # appeasement — A64-012.4 made this field nullable, and a `None`
        # here would mean the default had flipped to hiding every record.
        assert statistics is not None
        assert statistics.games_played == 0
        assert statistics.win_rate == 0.0

    async def test_statistics_are_none_when_the_player_hides_them(self) -> None:
        """A64-012.4, at the composition layer rather than over HTTP.

        `None` and not a zeroed record: zeroes are what a genuine beginner
        has, so publishing them for somebody who opted out would misinform
        the opponent deciding whether to accept a challenge.

        Ratings stay visible in the same response — UP-5, and the reason
        the flag is `show_statistics` rather than `show_ratings`.
        """
        hidden = account()
        hidden.privacy = hidden.privacy.updated(show_statistics=False)
        users = UserService(
            users=FakeUserRepository([hidden]),
            unit_of_work=_NullUnitOfWork(),
            clock=_FixedClock(),
        )
        service = ProfileService(
            profiles=PublicProfileService(users),
            ratings=UnratedRatingProvider(),
            statistics=NoMatchesStatisticsProvider(),
        )

        profile = await service.get_public_profile("Player_One")

        assert profile.statistics is None
        assert profile.ratings.as_map()[RatingCategory.BLITZ].rating == STARTING_RATING


class TestWinRate:
    """The one computed field. Derived on read and never stored — a
    persisted win rate is a number that can disagree with the four counts
    printed beside it."""

    @pytest.mark.parametrize(
        ("wins", "losses", "draws", "expected"),
        [
            pytest.param(0, 0, 0, 0.0, id="no games"),
            pytest.param(1, 0, 0, 1.0, id="only wins"),
            pytest.param(0, 1, 0, 0.0, id="only losses"),
            pytest.param(4, 4, 2, 0.4, id="draws dilute"),
            pytest.param(1, 2, 0, 0.3333, id="rounded to four places"),
        ],
    )
    def test_is_wins_over_games_played(
        self, wins: int, losses: int, draws: int, expected: float
    ) -> None:
        statistics = PlayerStatistics(
            games_played=wins + losses + draws, wins=wins, losses=losses, draws=draws
        )

        assert statistics.win_rate == expected

    def test_draws_are_in_the_denominator(self) -> None:
        """The product decision, asserted so that changing it to a chess
        score percentage is a deliberate act rather than a quiet one — the
        two disagree by a lot for a drawish player."""
        drawish = PlayerStatistics(games_played=100, wins=40, losses=20, draws=40)

        assert drawish.win_rate == 0.4

    def test_counts_that_do_not_sum_are_refused(self) -> None:
        """A broken backfill should fail one profile loudly rather than
        publish a win rate above 100% on every screen."""
        with pytest.raises(ValueError, match="must equal games_played"):
            PlayerStatistics(games_played=10, wins=9, losses=9, draws=0)

    def test_negative_counts_are_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            PlayerStatistics(games_played=1, wins=-1, losses=2, draws=0)


class TestUnratedDefaults:
    async def test_the_starting_rating_is_not_zero(self) -> None:
        """Zero would render as a real rating of the worst possible player
        rather than as "no measurement yet"."""
        ratings = PlayerRatings.unrated()

        assert ratings.classic.rating == STARTING_RATING > 0

    async def test_the_provider_ignores_the_player(self) -> None:
        """Honest signature: the answer is the same for everyone because no
        match has ever been recorded. The argument is accepted anyway so
        the placeholder stays substitutable for the real provider."""
        provider = UnratedRatingProvider()

        first = await provider.ratings_for(UUID(int=1))
        second = await provider.ratings_for(UUID(int=2))

        assert first == second
