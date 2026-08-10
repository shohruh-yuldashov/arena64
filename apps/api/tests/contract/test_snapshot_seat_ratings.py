"""The seat ratings survive the round trip into a live snapshot —
A64-025.6B.

The claim this task rests on is that publishing the seat ratings costs
nothing: the values are already on the match row, already read back by
`SqlAlchemyMatchRecordRepository`, and the reconnect path already loads that
record. So the snapshot carries them **without a second query and without
reading `rating` at all**.

That claim is about three components agreeing — the columns, the repository
mapping, and `GameMatchSnapshot` — which is exactly the seam an in-memory
double cannot model: a stub snapshot reader proves the gateway projects what
it is handed, not that the row hands it anything.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import generate_uuid7
from app.modules.engine import CURRENT_ENGINE_VERSION
from app.modules.game.application.services import GameMatchSnapshot, PersistedMatchReplay
from app.modules.game.domain.match_record import (
    MatchRecord,
    MatchRecordStatus,
    MatchSeat,
    SeatRating,
)
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.infrastructure.repositories import (
    SqlAlchemyMatchRecordRepository,
    SqlAlchemyMoveLogRepository,
)
from app.modules.game.public import engine_services
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
WINDOW = timedelta(seconds=30)


def _snapshots(session: AsyncSession) -> GameMatchSnapshot:
    """The real reconnect reader, assembled as the WebSocket route does —
    see `game.presentation.dependencies`."""
    repository = SqlAlchemyMatchRecordRepository(session)
    return GameMatchSnapshot(
        matches=repository,
        replays=PersistedMatchReplay(
            matches=repository, moves=SqlAlchemyMoveLogRepository(session)
        ),
        engine=engine_services().replay,
        clock=MovableClock(NOW),
    )


async def _active_match(
    matches: SqlAlchemyMatchRecordRepository,
    *,
    light: UUID,
    dark: UUID,
    light_rating: SeatRating | None,
    dark_rating: SeatRating | None,
) -> MatchRecord:
    """A match both players accepted, with whatever the seats rated.

    Untimed: nothing here depends on a clock, and an untimed match keeps
    the assertions about the thing this file is testing.
    """
    record = MatchRecord(
        pairing_id=generate_uuid7(),
        variant=ProductVariant.RUSSIAN_8X8,
        rated=True,
        engine_version=CURRENT_ENGINE_VERSION,
        light=MatchSeat(
            player_id=light,
            queue_ticket_id=generate_uuid7(),
            accepted_at=NOW,
            rating=light_rating,
        ),
        dark=MatchSeat(
            player_id=dark,
            queue_ticket_id=generate_uuid7(),
            accepted_at=NOW,
            rating=dark_rating,
        ),
        created_at=NOW,
        acceptance_deadline=NOW + WINDOW,
        status=MatchRecordStatus.ACTIVE,
        settled_at=NOW,
    )
    stored, _ = await matches.create(record)
    return stored


def _seat_rating(value: float, *, is_provisional: bool) -> SeatRating:
    return SeatRating(
        value=value,
        deviation=48.5,
        volatility=0.06,
        games_played=42,
        is_provisional=is_provisional,
        speed_class="blitz",
    )


class TestTheSnapshotCarriesWhatWasStored:
    async def test_the_seats_are_read_back_exactly_as_they_were_written(
        self, contract_session: AsyncSession
    ) -> None:
        """MT-4 and A64-025.6B — the values, unrounded and unrefreshed.

        Written by the creation path and read back by **another** component
        on the path a reconnecting client actually takes. A value the
        repository could hand back to its own caller proves nothing about a
        page refresh; the snapshot reader is what a refresh reaches.

        `pytest.approx` on the value, because it crosses a `Float` column
        and the assertion is "this number, not the opponent's" rather than
        "this bit pattern". `is_provisional` is asserted per side and with
        different values, so a projection that read one seat twice fails
        here rather than shipping.
        """
        matches = SqlAlchemyMatchRecordRepository(contract_session)
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(
            matches,
            light=light,
            dark=dark,
            light_rating=_seat_rating(1487.5, is_provisional=False),
            dark_rating=_seat_rating(1355.25, is_provisional=True),
        )
        await contract_session.commit()

        snapshot = await _snapshots(contract_session).snapshot_of(record.id)

        assert snapshot is not None
        assert snapshot.light_rating is not None
        assert snapshot.dark_rating is not None
        assert snapshot.light_rating.value == pytest.approx(1487.5)
        assert snapshot.dark_rating.value == pytest.approx(1355.25)
        assert not snapshot.light_rating.is_provisional
        assert snapshot.dark_rating.is_provisional
        # The whole triple survives, even though the wire publishes two of
        # it: PR-3's calculation runs on these, and a published type that
        # dropped them would make the completion path unimplementable.
        assert snapshot.light_rating.deviation == pytest.approx(48.5)
        assert snapshot.light_rating.speed_class == "blitz"

    async def test_a_match_stored_without_ratings_snapshots_as_none(
        self, contract_session: AsyncSession
    ) -> None:
        """Every match created before A64-017.2 has no seat rating.

        `None` rather than a zero or a default 1500: a fabricated rating in
        a permanent record is the mistake `MatchSeat.queue_ticket_id`
        already documents, and a client showing 1500 beside a player who
        was never rated would be stating something untrue.
        """
        matches = SqlAlchemyMatchRecordRepository(contract_session)
        light, dark = generate_uuid7(), generate_uuid7()
        record = await _active_match(
            matches, light=light, dark=dark, light_rating=None, dark_rating=None
        )
        await contract_session.commit()

        snapshot = await _snapshots(contract_session).snapshot_of(record.id)

        assert snapshot is not None
        assert snapshot.light_rating is None
        assert snapshot.dark_rating is None
