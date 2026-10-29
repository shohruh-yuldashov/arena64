"""Whose opponent a history row names — A64-032.

A history row's `opponent` was the **viewer's** opponent. On a player's own
history that is the right answer; on a profile, where the viewer is usually
somebody else, it was nobody on every row, and a "recent games" list could
only print "unknown" down its opponent column.

The row now reads from the seat of the player whose history it is. These
assert the mapping alone — no database, no route — because the defect was in
the mapping: the route only passes it an id.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.app_factory import create_app
from app.modules.auth.presentation.dependencies.current_user import get_current_user
from app.modules.auth.public.principal import AuthenticatedUser
from app.modules.game.domain.result import MatchOutcome, TerminationReason
from app.modules.game.presentation.dependencies import (
    get_match_rating_changes,
    get_replay_players,
    get_visible_history,
)
from app.modules.game.presentation.schemas.history import MatchHistoryResponse
from app.modules.game.public import (
    MatchHistoryEntry,
    MatchHistoryPage,
    PlayerSide,
    ProductVariant,
)

OWNER = UUID("00000000-0000-4000-8000-000000000001")
OPPONENT = UUID("00000000-0000-4000-8000-000000000002")
SECOND_OPPONENT = UUID("00000000-0000-4000-8000-000000000003")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _entry(match: int, *, light: UUID, dark: UUID) -> MatchHistoryEntry:
    return MatchHistoryEntry(
        match_id=UUID(f"00000000-0000-4000-9000-{match:012d}"),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=2,
        light_player_id=light,
        dark_player_id=dark,
        outcome=MatchOutcome.WIN,
        termination_reason=TerminationReason.RESIGNATION,
        winner=PlayerSide.LIGHT,
        time_control=None,
        speed_class="blitz",
        ply_number=40,
        ended_at=NOW,
        created_at=NOW,
    )


PAGE = MatchHistoryPage(
    entries=[
        _entry(1, light=OWNER, dark=OPPONENT),
        _entry(2, light=SECOND_OPPONENT, dark=OWNER),
        _entry(3, light=OWNER, dark=OPPONENT),
    ],
    next_cursor=None,
)


def test_each_row_names_the_owners_opponent_whichever_seat_the_owner_held() -> None:
    response = MatchHistoryResponse.of(PAGE, player_id=OWNER)

    assert [entry.opponent_id for entry in response.entries] == [
        OPPONENT,
        SECOND_OPPONENT,
        OPPONENT,
    ]


def test_an_opponent_whose_profile_could_not_be_read_is_present_without_a_name() -> None:
    # A deactivated account: `users` returns no public profile for it. The
    # row still says *who* by id and lets the client say "unknown player".
    response = MatchHistoryResponse.of(PAGE, player_id=OWNER, profiles={})

    first = response.entries[0].opponent
    assert first is not None
    assert first.player_id == OPPONENT
    assert first.username is None


def test_the_profile_lookup_asks_for_each_opponent_once_and_never_for_the_owner() -> None:
    assert MatchHistoryResponse.opponents_in(PAGE, player_id=OWNER) == sorted(
        [OPPONENT, SECOND_OPPONENT]
    )


# --- through the route ---------------------------------------------------------
#
# The mapping above was always right when handed the right id; the defect was
# the route handing it the *viewer's*. So the regression test is a request:
# a stranger reads the owner's history and must see the owner's opponents.
# Every collaborator is replaced through `dependency_overrides`, the seam
# `test_users_api.py` uses, so this needs no database. The client is not
# entered as a context manager: that runs the lifespan, whose outbox worker
# and presence reader would go looking for Postgres and Redis.


class _History:
    async def history_for(self, player_id, *, viewer_id, after, limit):  # type: ignore[no-untyped-def]
        assert player_id == OWNER
        return PAGE


class _Profiles:
    def __init__(self) -> None:
        self.asked: list[UUID] = []

    async def find_public_profiles(self, ids):  # type: ignore[no-untyped-def]
        self.asked = sorted(ids)
        return {}


class _RatingChanges:
    def __init__(self) -> None:
        self.asked_for: list[UUID] | None = None

    async def changes_for(self, player_id, match_ids):  # type: ignore[no-untyped-def]
        self.asked_for = list(match_ids)
        return {}


STRANGER = UUID("00000000-0000-4000-8000-000000000099")


def _client(viewer: UUID, profiles: _Profiles, ratings: _RatingChanges) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        id=viewer, token_id=uuid4(), issued_at=NOW, expires_at=NOW + timedelta(minutes=15)
    )
    app.dependency_overrides[get_visible_history] = _History
    app.dependency_overrides[get_replay_players] = lambda: profiles
    app.dependency_overrides[get_match_rating_changes] = lambda: ratings
    return TestClient(app)


def test_a_stranger_reading_a_history_sees_the_owners_opponents() -> None:
    profiles, ratings = _Profiles(), _RatingChanges()
    response = _client(STRANGER, profiles, ratings).get(f"/api/v1/players/{OWNER}/matches")

    assert response.status_code == 200, response.text
    entries = response.json()["data"]["entries"]
    assert [entry["opponent_id"] for entry in entries] == [
        str(OPPONENT),
        str(SECOND_OPPONENT),
        str(OPPONENT),
    ]
    assert profiles.asked == sorted([OPPONENT, SECOND_OPPONENT])


def test_a_stranger_is_still_never_shown_what_a_match_did_to_the_owners_rating() -> None:
    # The opponent moved to the owner's seat; the rating change did not. It
    # is served only to a player reading their own history — A64-023 §1.
    profiles, ratings = _Profiles(), _RatingChanges()
    response = _client(STRANGER, profiles, ratings).get(f"/api/v1/players/{OWNER}/matches")

    assert ratings.asked_for is None
    assert all(entry["rating"] is None for entry in response.json()["data"]["entries"])
