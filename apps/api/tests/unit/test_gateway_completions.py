"""A completed match reaches the players who were in it — A64-031.A.

`ClockAdjudicationService` settles a game nobody moved in, and it is right
not to hold a broadcaster: it has to work when both tabs are closed. The
consequence nobody had noticed is that when the tabs are *open*, the flag
reached neither of them. The worker completed the match, moved the ratings
and sent the notifications, and the two boards stayed exactly as they were
until somebody reloaded.

`GatewayCompletionSink` is the missing half: an outbox consumer that turns
`game.match_completed` into the `game.completed` frame the client already
knows, addressed to the participants the event already carries.

These prove the delivery path and the failure posture. That the adjudicator
produces the event at all is `tests/contract/test_live_clock.py`'s, against a
real PostgreSQL and a real Redis, and is not restated here.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.gateway.completions import CONSUMER_NAME, MATCH_COMPLETED, GatewayCompletionSink
from app.gateway.delivery import DeliveryReport
from app.gateway.protocol import GatewayMessage, MessageType
from app.platform.outbox.entry import OutboxEntry

LIGHT = UUID("11111111-1111-1111-1111-111111111111")
DARK = UUID("22222222-2222-2222-2222-222222222222")
MATCH = UUID("019fd1c7-5178-7a94-8076-4eeece03a8f4")


class _Broadcaster:
    """Records what would have gone on the wire."""

    def __init__(self, *, report: DeliveryReport | None = None, raises: bool = False) -> None:
        self.calls: list[tuple[GatewayMessage, tuple[UUID, ...]]] = []
        self._report = report or DeliveryReport(local=2, remote_nodes=0, failures=0)
        self._raises = raises

    async def deliver(
        self,
        message: GatewayMessage,
        *,
        recipients: Sequence[UUID],
        spectators: Sequence[Any] = (),
    ) -> DeliveryReport:
        if self._raises:
            raise RuntimeError("redis is unreachable")
        self.calls.append((message, tuple(recipients)))
        return self._report


class _Spectators:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises

    async def routes_for(self, match_id: UUID) -> Sequence[Any]:
        if self._raises:
            raise RuntimeError("cache is unreachable")
        return ()


class _Metrics:
    def __init__(self) -> None:
        self.counts: list[tuple[str, dict[str, str]]] = []

    def increment(self, name: str, *, by: int = 1, labels: dict[str, str] | None = None) -> None:
        self.counts.append((name, dict(labels or {})))

    def observe(self, name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
        pass

    def outcomes(self) -> list[str]:
        return [labels.get("outcome", "") for _, labels in self.counts]


def _seat(player_id: UUID) -> dict[str, Any]:
    return {
        "player_id": str(player_id),
        "rating_value": 1500.0,
        "rating_deviation": 350.0,
        "rating_volatility": 0.06,
        "games_played": 0,
        "is_provisional": True,
    }


def _entry(**overrides: Any) -> OutboxEntry:
    payload: dict[str, Any] = {
        "match_id": str(MATCH),
        "outcome": "win",
        "termination_reason": "flag",
        "winner": "dark",
        "ply_number": 31,
        "light": _seat(LIGHT),
        "dark": _seat(DARK),
    }
    payload.update(overrides)
    return OutboxEntry(
        id=uuid4(),
        aggregate_type="game.match",
        aggregate_id=MATCH,
        event_type=MATCH_COMPLETED,
        event_version=1,
        payload=payload,
        occurred_at=datetime(2026, 9, 7, 10, 30, tzinfo=UTC),
    )


def _sink(
    broadcaster: _Broadcaster, metrics: _Metrics, *, spectators: _Spectators | None = None
) -> GatewayCompletionSink:
    return GatewayCompletionSink(
        broadcaster=broadcaster,  # type: ignore[arg-type]
        spectators=spectators or _Spectators(),  # type: ignore[arg-type]
        metrics=metrics,  # type: ignore[arg-type]
    )


class TestTheSubscription:
    def test_takes_match_completions_and_nothing_else(self) -> None:
        sink = _sink(_Broadcaster(), _Metrics())
        assert sink.handles(MATCH_COMPLETED)
        assert not sink.handles("game.move_applied")
        assert not sink.handles("notification.created")

    def test_keeps_its_own_ledger_partition(self) -> None:
        # Five consumers now read `game.match_completed`, and none may mark
        # another's work done.
        assert CONSUMER_NAME == "gateway.match_completed"
        sink = _sink(_Broadcaster(), _Metrics())
        assert sink.consumer == CONSUMER_NAME

    def test_is_shaped_like_something_the_relay_will_accept(self) -> None:
        # `OutboxRelay` refuses a handler missing any of the three, and it
        # refuses it at construction — see `test_background_registry.py`. This
        # sink is appended to `handlers` unwrapped, holding no session, so its
        # conformance is its own rather than a wrapper's.
        sink = _sink(_Broadcaster(), _Metrics())
        assert isinstance(sink.consumer, str)
        assert callable(sink.handles)
        assert callable(sink.handle)

    def test_the_app_registers_it_against_the_completion_event(self) -> None:
        # The wiring, asserted from the composition root's own source: a sink
        # that is correct and unregistered fixes nothing, and nothing else in
        # the suite would notice its absence.
        from pathlib import Path

        factory = Path(__file__).resolve().parents[2] / "app" / "app_factory.py"
        source = factory.read_text()
        assert "GatewayCompletionSink(" in source, "the sink is never constructed"
        assert "handlers.append(\n        GatewayCompletionSink(" in source, (
            "the sink is constructed but not appended to the relay's handlers"
        )


class TestAFlagReachesBothPlayers:
    @pytest.mark.asyncio
    async def test_sends_game_completed_to_the_participants(self) -> None:
        broadcaster = _Broadcaster()
        sink = _sink(broadcaster, _Metrics())

        failures = await sink.handle([_entry()])

        assert list(failures) == []
        assert len(broadcaster.calls) == 1
        message, recipients = broadcaster.calls[0]
        assert message.type is MessageType.GAME_COMPLETED
        # Both seats, read off the event rather than from a repository — the
        # consumer runs in a process that may hold no `game` session at all.
        assert set(recipients) == {LIGHT, DARK}

    @pytest.mark.asyncio
    async def test_carries_the_result_shape_the_client_already_parses(self) -> None:
        broadcaster = _Broadcaster()
        sink = _sink(broadcaster, _Metrics())

        await sink.handle([_entry()])

        message, _ = broadcaster.calls[0]
        assert message.payload["match_id"] == str(MATCH)
        assert message.payload["ply"] == 31
        # The same three keys `game.move.applied` and `game.snapshot` carry,
        # so a reducer that already handles a resignation handles this.
        assert message.payload["result"] == {
            "outcome": "win",
            "termination_reason": "flag",
            "winner": "dark",
        }

    @pytest.mark.asyncio
    async def test_a_draw_carries_no_winner(self) -> None:
        broadcaster = _Broadcaster()
        sink = _sink(broadcaster, _Metrics())

        await sink.handle([_entry(outcome="draw", termination_reason="agreement", winner=None)])

        message, _ = broadcaster.calls[0]
        assert message.payload["result"]["winner"] is None


class TestItNeverFailsTheBatch:
    """It runs beside the rating and statistics consumers on one relay tick."""

    @pytest.mark.asyncio
    async def test_an_unreadable_payload_is_skipped_rather_than_retried(self) -> None:
        broadcaster = _Broadcaster()
        metrics = _Metrics()
        sink = _sink(broadcaster, metrics)

        failures = await sink.handle([_entry(match_id="not-a-uuid")])

        assert list(failures) == []
        assert broadcaster.calls == []
        assert "unreadable" in metrics.outcomes()

    @pytest.mark.asyncio
    async def test_a_match_with_no_seats_is_skipped(self) -> None:
        # Created before seat summaries existed: there is nobody to address,
        # and inventing a recipient is worse than sending nothing.
        broadcaster = _Broadcaster()
        sink = _sink(broadcaster, _Metrics())

        await sink.handle([_entry(light=None, dark=None)])

        assert broadcaster.calls == []

    @pytest.mark.asyncio
    async def test_a_fan_out_failure_is_counted_not_raised(self) -> None:
        metrics = _Metrics()
        sink = _sink(_Broadcaster(raises=True), metrics)

        failures = await sink.handle([_entry()])

        assert list(failures) == []
        assert "failed" in metrics.outcomes()

    @pytest.mark.asyncio
    async def test_an_unreadable_entry_does_not_stop_the_ones_beside_it(self) -> None:
        broadcaster = _Broadcaster()
        sink = _sink(broadcaster, _Metrics())

        await sink.handle([_entry(match_id="not-a-uuid"), _entry(), _entry()])

        assert len(broadcaster.calls) == 2

    @pytest.mark.asyncio
    async def test_an_unreadable_audience_still_tells_the_players(self) -> None:
        # A viewer list that could not be read must not cost the two people
        # whose game it was the news that it ended.
        broadcaster = _Broadcaster()
        sink = _sink(broadcaster, _Metrics(), spectators=_Spectators(raises=True))

        await sink.handle([_entry()])

        assert len(broadcaster.calls) == 1


class TestWhatTheCounterSays:
    @pytest.mark.asyncio
    async def test_nobody_connected_is_an_outcome_rather_than_a_failure(self) -> None:
        # A game both players left still ends, and `game.snapshot` carries
        # the result to whichever of them opens it next.
        metrics = _Metrics()
        sink = _sink(
            _Broadcaster(report=DeliveryReport(local=0, remote_nodes=0, failures=2)), metrics
        )

        await sink.handle([_entry()])

        assert "nobody" in metrics.outcomes()
        assert "failed" not in metrics.outcomes()

    @pytest.mark.asyncio
    async def test_a_frame_written_to_a_peer_node_counts_as_delivered(self) -> None:
        metrics = _Metrics()
        sink = _sink(
            _Broadcaster(report=DeliveryReport(local=0, remote_nodes=1, failures=0)), metrics
        )

        await sink.handle([_entry()])

        assert "delivered" in metrics.outcomes()
