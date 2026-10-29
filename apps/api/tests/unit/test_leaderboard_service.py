"""Who is on a leaderboard, in what order, and at what cost — A64-033.

`LeaderboardService` composes `rating`'s ladder with `users`' public
profiles and `admin`'s restrictions. Every rule it adds is asserted here
against fakes of those three ports, so these run without a database; the
SQL behind the ladder is `tests/contract/test_leaderboard.py`'s.

The fake ladder keeps the published order exactly — rating descending,
deviation ascending, player id ascending — and the same keyset semantics,
so a test that passes here is about the composition, not about a fake that
orders differently from the real thing.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.app_factory import create_app
from app.core.enums import Locale
from app.modules.auth.presentation.dependencies.current_user import get_current_user
from app.modules.auth.public.principal import AuthenticatedUser
from app.modules.game.public import ProductVariant
from app.modules.profiles.application.services.leaderboard_service import (
    SCAN_BUDGET,
    LeaderboardService,
    StandingStatus,
)
from app.modules.profiles.presentation.dependencies import get_leaderboard_service
from app.modules.profiles.presentation.schemas.leaderboard import encode_position
from app.modules.rating.public import (
    LeaderboardCursor,
    LeaderboardEntry,
    LeaderboardPage,
    RatingKey,
    SpeedClass,
)
from app.modules.rating.public.leaderboard import LeaderboardNeighbourhood
from app.modules.users.public import (
    AvatarReference,
    ProfileVisibility,
    PublicUserProfile,
    VisibilityLevel,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
KEY = RatingKey(variant=ProductVariant.RUSSIAN_8X8, speed_class=SpeedClass.BLITZ)


def _id(n: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


def _entry(n: int, rating: float, *, deviation: float = 60.0, games: int = 40) -> LeaderboardEntry:
    return LeaderboardEntry(
        player_id=_id(n),
        rating=rating,
        deviation=deviation,
        games_played=games,
        is_provisional=games < 25,
    )


def _profile(n: int, *, country: str | None = None) -> PublicUserProfile:
    """`country` is what `users` publishes — already `None` when hidden."""
    return PublicUserProfile(
        id=_id(n),
        username=f"player{n}",
        display_name=f"Player {n}",
        avatar=AvatarReference(object_key=None, version=0, uploaded_at=None),
        country=country,
        preferred_language=Locale.EN,
        bio=None,
        created_at=NOW,
        visibility=ProfileVisibility(
            last_seen=VisibilityLevel.NOBODY,
            statistics=False,
            online_status=VisibilityLevel.NOBODY,
            activity=VisibilityLevel.NOBODY,
        ),
    )


def _order(entry: LeaderboardEntry) -> tuple[float, float, UUID]:
    return (-entry.rating, entry.deviation, entry.player_id)


class _Ladder:
    """`rating.public.LeaderboardReader`, in memory, one key."""

    def __init__(self, entries: Sequence[LeaderboardEntry]) -> None:
        self.rows = sorted(entries, key=_order)
        self.page_calls = 0

    async def page(
        self, key: RatingKey, *, after: LeaderboardCursor | None = None, limit: int = 50
    ) -> LeaderboardPage:
        self.page_calls += 1
        rows = self.rows
        if after is not None:
            mark = (-after.rating, after.deviation, after.player_id)
            rows = [row for row in rows if _order(row) > mark]
        window = rows[: limit + 1]
        page = window[:limit]
        last = page[-1] if page else None
        return LeaderboardPage(
            entries=page,
            next_cursor=(
                LeaderboardCursor(
                    rating=last.rating, deviation=last.deviation, player_id=last.player_id
                )
                if last is not None and len(window) > limit
                else None
            ),
        )

    async def around(
        self, player_id: UUID, *, key: RatingKey, span: int = 5
    ) -> LeaderboardNeighbourhood | None:
        for index, row in enumerate(self.rows):
            if row.player_id == player_id:
                return LeaderboardNeighbourhood(rank=index + 1, entry=row, above=[], below=[])
        return None


class _Profiles:
    def __init__(self, profiles: Sequence[PublicUserProfile]) -> None:
        self._profiles = {profile.id: profile for profile in profiles}
        self.calls: list[int] = []

    async def find_public_profile(self, username: str) -> PublicUserProfile | None:
        return next((p for p in self._profiles.values() if p.username == username), None)

    async def find_public_profiles(
        self, player_ids: Sequence[UUID]
    ) -> Mapping[UUID, PublicUserProfile]:
        self.calls.append(len(player_ids))
        return {pid: self._profiles[pid] for pid in player_ids if pid in self._profiles}


class _Restricted:
    def __init__(self, restricted: set[UUID] | None = None) -> None:
        self._restricted = frozenset(restricted or set())
        self.calls = 0

    async def restricted_among(
        self, player_ids: Sequence[UUID], *, at: datetime
    ) -> frozenset[UUID]:
        self.calls += 1
        return self._restricted & frozenset(player_ids)


class _Clock:
    def now(self) -> datetime:
        return NOW


def _service(
    entries: Sequence[LeaderboardEntry],
    profiles: Sequence[PublicUserProfile],
    restricted: set[UUID] | None = None,
) -> tuple[LeaderboardService, _Ladder, _Profiles, _Restricted]:
    ladder, users, admin = _Ladder(entries), _Profiles(profiles), _Restricted(restricted)
    return (
        LeaderboardService(ladder=ladder, profiles=users, restrictions=admin, clock=_Clock()),
        ladder,
        users,
        admin,
    )


def _ranked(view) -> list[tuple[int, UUID]]:  # type: ignore[no-untyped-def]
    return [(player.rank, player.entry.player_id) for player in view.players]


# --- ordering and ties -------------------------------------------------------


async def test_it_ranks_by_rating_then_certainty_then_id() -> None:
    service, *_ = _service(
        [
            _entry(1, 1500.0),
            _entry(2, 1700.0),
            # Same rating as 4, less certain: ranks below it.
            _entry(3, 1600.0, deviation=120.0),
            _entry(4, 1600.0, deviation=60.0),
            # Same rating *and* deviation as 4: the id decides, so the order
            # is total and a cursor can never skip or repeat a player.
            _entry(5, 1600.0, deviation=60.0),
        ],
        [_profile(n) for n in range(1, 6)],
    )

    view = await service.page(KEY, country=None, after=None, limit=10)

    assert _ranked(view) == [(1, _id(2)), (2, _id(4)), (3, _id(5)), (4, _id(3)), (5, _id(1))]
    assert view.next is None


# --- pagination --------------------------------------------------------------


async def test_pages_continue_the_ranks_without_skipping_or_repeating_anybody() -> None:
    service, *_ = _service(
        [_entry(n, 2000.0 - n) for n in range(1, 6)], [_profile(n) for n in range(1, 6)]
    )

    first = await service.page(KEY, country=None, after=None, limit=2)
    second = await service.page(KEY, country=None, after=first.next, limit=2)
    third = await service.page(KEY, country=None, after=second.next, limit=2)

    assert [rank for rank, _ in _ranked(first) + _ranked(second) + _ranked(third)] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert [pid for _, pid in _ranked(first) + _ranked(second) + _ranked(third)] == [
        _id(n) for n in range(1, 6)
    ]
    assert third.next is None


async def test_an_empty_ladder_is_an_empty_page_with_nowhere_to_go() -> None:
    service, *_ = _service([], [])

    view = await service.page(KEY, country=None, after=None, limit=50)

    assert view.players == []
    assert view.next is None


# --- who is shown ------------------------------------------------------------


async def test_provisional_players_are_ranked_and_marked() -> None:
    service, *_ = _service([_entry(1, 1500.0, games=3)], [_profile(1)])

    view = await service.page(KEY, country=None, after=None, limit=10)

    assert [player.entry.is_provisional for player in view.players] == [True]


async def test_restricted_and_deactivated_accounts_are_left_out_and_ranks_close_up() -> None:
    # Player 2 has no public profile (deactivated or erased); player 3 is
    # restricted. Neither takes a rank, and nobody below them loses one.
    service, *_ = _service(
        [_entry(n, 2000.0 - n) for n in range(1, 5)],
        [_profile(1), _profile(3), _profile(4)],
        restricted={_id(3)},
    )

    view = await service.page(KEY, country=None, after=None, limit=10)

    assert _ranked(view) == [(1, _id(1)), (2, _id(4))]


async def test_a_country_ladder_shows_only_players_whose_visible_country_it_is() -> None:
    service, *_ = _service(
        [_entry(n, 2000.0 - n) for n in range(1, 5)],
        [
            _profile(1, country="KZ"),
            _profile(2, country="UZ"),
            # Hidden by its owner — `users` publishes `None` — or never set.
            # Either way this player is on no country's ladder.
            _profile(3, country=None),
            _profile(4, country="UZ"),
        ],
    )

    uz = await service.page(KEY, country="UZ", after=None, limit=10)
    everyone = await service.page(KEY, country=None, after=None, limit=10)

    assert _ranked(uz) == [(1, _id(2)), (2, _id(4))]
    assert [pid for _, pid in _ranked(everyone)] == [_id(n) for n in range(1, 5)]


# --- cost --------------------------------------------------------------------


async def test_a_page_checks_its_rows_in_one_batch_each_never_one_per_player() -> None:
    service, ladder, users, admin = _service(
        [_entry(n, 2000.0 - n) for n in range(1, 41)], [_profile(n) for n in range(1, 41)]
    )

    await service.page(KEY, country=None, after=None, limit=20)

    assert (ladder.page_calls, len(users.calls), admin.calls) == (1, 1, 1)


async def test_a_sparse_country_stops_at_the_budget_and_hands_back_a_cursor() -> None:
    # Everybody near the top is from elsewhere; the one UZ player is deep.
    depth = SCAN_BUDGET + 50
    entries = [_entry(n, 5000.0 - n) for n in range(1, depth + 1)]
    profiles = [_profile(n, country="KZ") for n in range(1, depth)] + [
        _profile(depth, country="UZ")
    ]
    service, ladder, *_ = _service(entries, profiles)

    first = await service.page(KEY, country="UZ", after=None, limit=50)
    calls_for_first = ladder.page_calls
    second = await service.page(KEY, country="UZ", after=first.next, limit=50)

    # Bounded: the first request stopped at the budget rather than scanning on.
    assert first.players == []
    assert first.next is not None
    assert calls_for_first <= SCAN_BUDGET // 200 + 1
    # And the walk resumes where it stopped, with the rank still right.
    assert _ranked(second) == [(1, _id(depth))]


# --- the caller's own place --------------------------------------------------


async def test_your_rank_counts_only_the_players_shown_above_you() -> None:
    service, *_ = _service(
        [_entry(n, 2000.0 - n) for n in range(1, 5)],
        [_profile(1), _profile(3), _profile(4)],  # 2 is deactivated
        restricted={_id(1)},
    )

    standing = await service.standing(_id(4), KEY, country=None)

    assert (standing.status, standing.rank) == (StandingStatus.RANKED, 2)


async def test_you_are_unrated_in_a_class_you_have_not_played() -> None:
    service, *_ = _service([_entry(1, 1500.0)], [_profile(1), _profile(2)])

    standing = await service.standing(_id(2), KEY, country=None)

    assert (standing.status, standing.rank, standing.entry) == (StandingStatus.UNRATED, None, None)


@pytest.mark.parametrize("own_country", [None, "KZ"])
async def test_you_are_not_listed_on_a_country_ladder_your_profile_does_not_show(
    own_country: str | None,
) -> None:
    service, *_ = _service([_entry(1, 1500.0)], [_profile(1, country=own_country)])

    standing = await service.standing(_id(1), KEY, country="UZ")

    assert standing.status is StandingStatus.NOT_LISTED
    assert standing.rank is None
    # Your own rating is still yours to see.
    assert standing.entry is not None


async def test_far_down_the_ladder_you_get_your_rating_rather_than_an_unbounded_count() -> None:
    depth = SCAN_BUDGET + 10
    service, ladder, *_ = _service(
        [_entry(n, 5000.0 - n) for n in range(1, depth + 1)],
        [_profile(n) for n in range(1, depth + 1)],
    )

    standing = await service.standing(_id(depth), KEY, country=None)

    assert standing.status is StandingStatus.BEYOND
    assert ladder.page_calls == 0


# --- through the routes ------------------------------------------------------

CALLER = _id(1)


def _client(service: LeaderboardService) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        id=CALLER, token_id=uuid4(), issued_at=NOW, expires_at=NOW + timedelta(minutes=15)
    )
    app.dependency_overrides[get_leaderboard_service] = lambda: service
    # Not entered: the lifespan would start workers that look for Postgres.
    return TestClient(app)


def test_the_route_serves_names_visible_countries_and_ranks() -> None:
    service, *_ = _service(
        [_entry(1, 1600.0), _entry(2, 1500.0, games=3)],
        [_profile(1, country="UZ"), _profile(2)],
    )

    body = _client(service).get("/api/v1/leaderboard/players", params={"speed_class": "blitz"})

    assert body.status_code == 200, body.text
    players = body.json()["data"]["players"]
    assert [(p["rank"], p["username"], p["country"]) for p in players] == [
        (1, "player1", "UZ"),
        (2, "player2", None),
    ]
    assert players[1]["is_provisional"] is True
    # Nothing beyond the public face crosses: no email, no avatar key.
    assert set(players[0]) == {
        "rank",
        "player_id",
        "username",
        "display_name",
        "country",
        "rating",
        "games_played",
        "is_provisional",
    }


def test_the_country_filter_is_case_insensitive_and_echoed_upper_case() -> None:
    service, *_ = _service([_entry(1, 1600.0)], [_profile(1, country="UZ")])

    body = _client(service).get("/api/v1/leaderboard/players", params={"country": "uz"}).json()

    assert body["data"]["country"] == "UZ"
    assert len(body["data"]["players"]) == 1


def test_a_country_that_is_not_two_letters_is_refused() -> None:
    service, *_ = _service([], [])

    assert (
        _client(service).get("/api/v1/leaderboard/players", params={"country": "UZB"}).status_code
        == 422
    )


def test_a_cursor_this_api_did_not_issue_is_refused() -> None:
    service, *_ = _service([], [])

    response = _client(service).get("/api/v1/leaderboard/players", params={"after": "not-a-cursor"})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_cursor"


def test_a_cursor_round_trips_through_the_route() -> None:
    service, *_ = _service(
        [_entry(n, 2000.0 - n) for n in range(1, 4)], [_profile(n) for n in range(1, 4)]
    )
    client = _client(service)

    first = client.get("/api/v1/leaderboard/players", params={"limit": 2}).json()["data"]
    second = client.get(
        "/api/v1/leaderboard/players", params={"limit": 2, "after": first["next_cursor"]}
    ).json()["data"]

    assert [p["rank"] for p in first["players"] + second["players"]] == [1, 2, 3]
    assert second["next_cursor"] is None


def test_your_standing_is_read_for_the_token_holder_only() -> None:
    service, *_ = _service([_entry(2, 1700.0), _entry(1, 1600.0)], [_profile(1), _profile(2)])

    body = (
        _client(service)
        .get("/api/v1/leaderboard/players/me", params={"player_id": str(_id(2))})
        .json()["data"]
    )

    assert (body["status"], body["rank"]) == ("ranked", 2)


def test_the_cursor_encoding_carries_the_rank_so_far() -> None:
    # Guard against the encoding silently dropping the count: a cursor that
    # lost it would restart numbering at 1 on page two.
    from app.modules.profiles.application.services.leaderboard_service import LadderPosition
    from app.modules.profiles.presentation.schemas.leaderboard import decode_position

    position = LadderPosition(
        after=LeaderboardCursor(rating=1500.5, deviation=60.0, player_id=_id(9)), ranked=37
    )

    assert decode_position(encode_position(position)) == position
