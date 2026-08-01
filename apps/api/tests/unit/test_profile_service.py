"""`ProfileService` and the `profiles` domain — composition, no database.

A64-012.1 asks for essential tests only, and names three: successful
lookup, profile not found, and case-insensitive lookup. All three are here
and again in `tests/contract/test_profiles_api.py` end to end; this file is
the fast half and adds the two properties that are cheaper to assert
without HTTP.

A64-012.7 names five more, all of them about presence — online, offline,
hidden online status, hidden last seen, and the owner's own view — and all
five are in `TestPresence` below. They belong here rather than in
`test_presence.py` because every one of them is a statement about *the
composition*: which flag gates which field, and what a stranger is told when
a flag is off. `test_presence.py` covers the adapter underneath.

Runs the **real** `UserService` and `PublicProfileService` over
`FakeUserRepository`, which the contract suite in
`tests/contract/test_user_repository.py` holds to the same behaviour as the
SQLAlchemy adapter. That matters for the case-insensitivity test in
particular: folding is the repository's, so a hand-rolled fake reader would
have proven only that the fake folds.

The rating, statistics and presence providers are the production ones too —
the last over a fake Redis client rather than a fake provider, so the
privacy gate is exercised against the same decode path that runs in
production. Substituting a provider would leave the composition untested,
which is the one thing this file exists to test.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Self, cast
from uuid import UUID

import pytest
from redis.asyncio import Redis

from app.config.settings import PresenceSettings
from app.core.enums import Locale
from app.modules.profiles.application.services import ProfileService
from app.modules.profiles.application.services.profile_composer import PublicProfileComposer
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
from app.modules.users.infrastructure.presence import RedisPresenceProvider
from app.modules.users.public import Presence, PresenceProvider, VisibilityLevel
from tests.fakes.presence_redis import FakePresenceRedis, MovableClock
from tests.fakes.user_repository import FakeUserRepository

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

#: Long enough that no test in this file reaches it by accident, and
#: irrelevant to every assertion here — expiry is `test_presence.py`'s.
PRESENCE_TTL_SECONDS = 300


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
def presence_clock() -> MovableClock:
    return MovableClock(NOW)


@pytest.fixture
def presence_store(presence_clock: MovableClock) -> FakePresenceRedis:
    """Empty by default — nobody has been observed, which is the state of
    every account until AD-09's gateway exists. A test that needs somebody
    online records it through the provider below."""
    return FakePresenceRedis(presence_clock)


@pytest.fixture
def presence(
    presence_store: FakePresenceRedis, presence_clock: MovableClock
) -> RedisPresenceProvider:
    # The real adapter over a fake client — see this module's docstring, and
    # `tests/unit/test_presence.py` for why the `cast` rather than a port
    # over the driver.
    return RedisPresenceProvider(
        cast(Redis, presence_store),
        settings=PresenceSettings(ttl_seconds=PRESENCE_TTL_SECONDS),
        clock=presence_clock,
    )


def build_service(accounts: list[User], presence: PresenceProvider) -> ProfileService:
    """The production graph over in-memory storage.

    A helper rather than a fixture because half the tests below need an
    account in a state the `user` fixture does not produce — deactivated,
    or with a privacy flag flipped — and parameterising a fixture for each
    would put the interesting part of each test somewhere other than the
    test.
    """
    users = UserService(
        users=FakeUserRepository(accounts),
        unit_of_work=_NullUnitOfWork(),
        clock=_FixedClock(),
    )
    statistics = NoMatchesStatisticsProvider()
    return ProfileService(
        profiles=PublicProfileService(users),
        # The real composer over the real providers — A64-013.1 moved the
        # privacy gate there, and substituting it would leave the thing
        # these tests exist to assert untested.
        composer=PublicProfileComposer(
            ratings=UnratedRatingProvider(),
            statistics=statistics,
            presence=presence,
        ),
        statistics=statistics,
        presence=presence,
    )


@pytest.fixture
def service(user: User, presence: RedisPresenceProvider) -> ProfileService:
    return build_service([user], presence)


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

    async def test_presence_is_absent_rather_than_invented(self, service: ProfileService) -> None:
        """Nothing stored — not `updated_at`, not a session's
        `last_used_at` — may stand in for presence. With nothing recorded,
        both fields are `None`, which is what every account reports until
        AD-09's gateway writes one."""
        profile = await service.get_public_profile("Player_One")

        assert profile.is_online is None
        assert profile.last_seen is None


class TestProfileNotFound:
    async def test_an_unknown_username_raises(self, service: ProfileService) -> None:
        with pytest.raises(ProfileNotFound):
            await service.get_public_profile("nobody_here")

    async def test_a_deactivated_account_is_indistinguishable_from_missing(
        self, presence: RedisPresenceProvider
    ) -> None:
        """Publishing *which* accounts are withdrawn tells an impersonator
        whose handle is safe to adopt. Asserted as an equality between the
        two rejections rather than against a constant, because the failure
        being guarded against is a difference, whatever form it takes."""
        service = build_service([account(is_active=False)], presence)

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

    async def test_statistics_are_none_when_the_player_hides_them(
        self, presence: RedisPresenceProvider
    ) -> None:
        """A64-012.4, at the composition layer rather than over HTTP.

        `None` and not a zeroed record: zeroes are what a genuine beginner
        has, so publishing them for somebody who opted out would misinform
        the opponent deciding whether to accept a challenge.

        Ratings stay visible in the same response — UP-5, and the reason
        the flag is `show_statistics` rather than `show_ratings`.
        """
        hidden = account()
        hidden.privacy = hidden.privacy.updated(show_statistics=False)
        service = build_service([hidden], presence)

        profile = await service.get_public_profile("Player_One")

        assert profile.statistics is None
        assert profile.ratings.as_map()[RatingCategory.BLITZ].rating == STARTING_RATING


class TestPresence:
    """The five cases A64-012.7 names, plus the one property that ties them
    together: a `null` never says why.

    Every test runs the production graph — the real `RedisPresenceProvider`
    over a fake Redis client — so what is being asserted is the *gate*, not
    a stubbed answer to it.
    """

    async def test_an_online_player_is_reported_as_online(
        self, service: ProfileService, presence: RedisPresenceProvider, user: User
    ) -> None:
        """`show_online_status` is on by default, because a challenge sent
        to a player who is not there is a challenge that expires."""
        await presence.record_presence(user.id, is_online=True)

        assert (await service.get_public_profile("Player_One")).is_online is True

    async def test_an_offline_player_is_reported_as_offline(
        self, service: ProfileService, presence: RedisPresenceProvider, user: User
    ) -> None:
        """`False` rather than `None`, and the distinction is the whole
        reason a disconnect is *recorded* instead of deleting the key: the
        platform saw this player leave, recently enough to still be able to
        say so."""
        await presence.record_presence(user.id, is_online=False)

        assert (await service.get_public_profile("Player_One")).is_online is False

    async def test_a_hidden_online_status_is_null_even_while_the_player_is_online(
        self, presence: RedisPresenceProvider, user: User
    ) -> None:
        """UP-4: enforced server-side, on the read path, from a flag the
        response never carries. The record says `True` and the profile says
        nothing."""
        user.privacy = user.privacy.updated(online_status=VisibilityLevel.NOBODY)
        service = build_service([user], presence)
        await presence.record_presence(user.id, is_online=True)

        assert (await service.get_public_profile("Player_One")).is_online is None

    async def test_a_hidden_last_seen_is_null_even_while_presence_is_recorded(
        self, presence: RedisPresenceProvider, user: User
    ) -> None:
        """`show_last_seen` is the one privacy flag that is off by default,
        so this is the *ordinary* case rather than an edge one — a
        timestamp published for months is a sleep schedule, while "online
        now" is momentary."""
        await presence.record_presence(user.id, is_online=True)
        service = build_service([user], presence)

        profile = await service.get_public_profile("Player_One")

        assert profile.last_seen is None
        # The default account shows its online status, so the two flags are
        # visibly independent: hiding one does not hide the other, which is
        # exactly why they are two flags.
        assert profile.is_online is True

    async def test_the_owner_always_sees_their_own_presence(
        self, presence: RedisPresenceProvider, user: User
    ) -> None:
        """A64-012.7. Both flags off, and the owner still sees both fields:
        a control that hid a player's presence from the player would be one
        nobody could verify they had set.

        Asserted through `get_own_presence`, which is what `GET /profile/me`
        calls — and which deliberately takes a `player_id` the caller has
        already authenticated rather than a username, so there is no path by
        which it returns somebody else's.
        """
        user.privacy = user.privacy.updated(
            online_status=VisibilityLevel.NOBODY,
            last_seen=VisibilityLevel.NOBODY,
        )
        service = build_service([user], presence)
        await presence.record_presence(user.id, is_online=True)

        own = await service.get_own_presence(user.id)

        assert own is not None
        assert own.is_online is True
        assert own.last_seen == NOW

        # The same account, seen by a stranger in the same moment: both
        # fields gone. The pair is the assertion — either half alone would
        # pass with the gate applied in the wrong place.
        stranger = await service.get_public_profile("Player_One")
        assert stranger.is_online is None
        assert stranger.last_seen is None

    async def test_hidden_is_indistinguishable_from_never_recorded(
        self, presence: RedisPresenceProvider, user: User
    ) -> None:
        """A64-012.7's central constraint: the response must not let a
        client tell "hidden" from "unavailable" from "not yet recorded".

        Asserted as an equality between two profiles rather than against
        `None`, because the failure being guarded against is a *difference*
        — whatever form a future marker, placeholder or sentinel might
        take.
        """
        hidden = account(username="Hidden_One")
        hidden.privacy = hidden.privacy.updated(
            online_status=VisibilityLevel.NOBODY,
            last_seen=VisibilityLevel.NOBODY,
        )
        service = build_service([hidden, user], presence)
        await presence.record_presence(hidden.id, is_online=True)

        opted_out = await service.get_public_profile("Hidden_One")
        never_observed = await service.get_public_profile("Player_One")

        assert (opted_out.is_online, opted_out.last_seen) == (
            never_observed.is_online,
            never_observed.last_seen,
        )

    async def test_a_player_who_hid_everything_is_not_looked_up_at_all(
        self, presence: RedisPresenceProvider, user: User
    ) -> None:
        """Not fetched rather than fetched and discarded — the rule the
        statistics read follows. A value never loaded cannot be leaked by a
        later mapper that forgets a flag, and the platform does no work on
        behalf of somebody who opted out of both.

        Asserted by making a read *fail*: an unreachable store is invisible
        precisely when nothing consults it.
        """
        hidden = account()
        hidden.privacy = hidden.privacy.updated(
            online_status=VisibilityLevel.NOBODY,
            last_seen=VisibilityLevel.NOBODY,
        )
        service = build_service([hidden], _ExplodingPresenceProvider())

        profile = await service.get_public_profile("Player_One")

        assert profile.is_online is None
        assert profile.last_seen is None


class _ExplodingPresenceProvider:
    """A provider that must never be called.

    Sharper than a spy counting calls: a spy asserts what happened *after*
    the fact and still runs the read, while this makes the read itself the
    failure. Deliberately raises rather than returning a sentinel, because
    `PresenceProvider` promises never to raise — so a test that sees this
    escape has proven the call happened, not that the provider is broken.
    """

    async def presence_for(self, player_id: UUID) -> None:
        raise AssertionError("presence must not be read for a player who has hidden both fields")

    async def presence_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, Presence]:
        """The batch half of the same trap — the composer must not consult
        presence for a page whose every member has hidden it either."""
        raise AssertionError("presence must not be read for players who have hidden both fields")


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
