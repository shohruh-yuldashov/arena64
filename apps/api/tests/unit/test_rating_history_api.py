"""`GET /ratings/me/history` — A64-032.

What the route owns: whose history it reads (the token's, and nothing in
the request can change that), the query-parameter conversion, the bounds
on `limit`, and the wire shape. The SQL is `tests/contract`'s — see
`test_rating_api.py::TestRatingHistory`.

The client is not entered as a context manager, so the app's lifespan —
and the workers it starts against Postgres and Redis — never runs.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.app_factory import create_app
from app.modules.auth.presentation.dependencies.current_user import get_current_user
from app.modules.auth.public.principal import AuthenticatedUser
from app.modules.game.public import ProductVariant
from app.modules.rating.presentation.dependencies import get_rating_history_reader
from app.modules.rating.public import (
    RatingChange,
    RatingHistory,
    RatingHistoryPoint,
    RatingKey,
    SpeedClass,
)

CALLER = UUID("00000000-0000-4000-8000-000000000001")
SOMEBODY_ELSE = UUID("00000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
URL = "/api/v1/ratings/me/history"


class _Reader:
    def __init__(self, history: RatingHistory) -> None:
        self._history = history
        self.calls: list[tuple[UUID, RatingKey, int]] = []

    async def history_for(self, player_id: UUID, *, key: RatingKey, limit: int) -> RatingHistory:
        self.calls.append((player_id, key, limit))
        return self._history


def _client(reader: _Reader) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        id=CALLER, token_id=uuid4(), issued_at=NOW, expires_at=NOW + timedelta(minutes=15)
    )
    app.dependency_overrides[get_rating_history_reader] = lambda: reader
    return TestClient(app)


def _point(match: int, before: float, after: float) -> RatingHistoryPoint:
    return RatingHistoryPoint(
        match_id=UUID(f"00000000-0000-4000-9000-{match:012d}"),
        applied_at=NOW + timedelta(hours=match),
        change=RatingChange.of(before=before, after=after),
    )


EMPTY = RatingHistory(points=[], has_earlier=False)


def test_it_reads_the_callers_history_whatever_the_request_names() -> None:
    reader = _Reader(EMPTY)

    # A `player_id` in the query is not a parameter of this route; it must
    # change nothing, rather than read somebody else's line.
    response = _client(reader).get(URL, params={"player_id": str(SOMEBODY_ELSE)})

    assert response.status_code == 200, response.text
    assert [call[0] for call in reader.calls] == [CALLER]


def test_it_reads_the_key_and_limit_asked_for() -> None:
    reader = _Reader(EMPTY)

    _client(reader).get(URL, params={"speed_class": "blitz", "limit": 20})

    assert reader.calls == [
        (CALLER, RatingKey(variant=ProductVariant.RUSSIAN_8X8, speed_class=SpeedClass.BLITZ), 20)
    ]


def test_it_serves_each_point_as_rounded_integers_with_a_server_side_delta() -> None:
    reader = _Reader(
        RatingHistory(
            points=[_point(1, 1500.0, 1512.6), _point(2, 1512.6, 1498.4)],
            has_earlier=True,
        )
    )

    body = _client(reader).get(URL, params={"speed_class": "blitz"}).json()["data"]

    assert body["speed_class"] == "blitz"
    assert body["has_earlier"] is True
    assert [(p["before"], p["after"], p["delta"]) for p in body["points"]] == [
        (1500, 1513, 13),
        (1513, 1498, -15),
    ]


def test_a_key_never_played_is_an_empty_line_not_an_error() -> None:
    response = _client(_Reader(EMPTY)).get(URL)

    assert response.status_code == 200
    assert response.json()["data"]["points"] == []


def test_it_refuses_a_limit_outside_its_bounds_before_reading_anything() -> None:
    reader = _Reader(EMPTY)
    client = _client(reader)

    assert client.get(URL, params={"limit": 0}).status_code == 422
    assert client.get(URL, params={"limit": 201}).status_code == 422
    assert reader.calls == []


def test_it_refuses_a_speed_class_that_does_not_exist() -> None:
    assert _client(_Reader(EMPTY)).get(URL, params={"speed_class": "hyper"}).status_code == 422
