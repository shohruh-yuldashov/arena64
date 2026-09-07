"""What a client is told the clock says — A64-031.A.

A player watched their clock read 29:05, thought for a minute, reloaded, and
saw 29:05 again. The minute they had spent came back.

`ClockState` was never wrong. It stores a remaining duration plus
`turn_started_at`, and everything authoritative is derived from that pair:
`deadline` is `turn_started_at + remaining`, `charged` deducts only when a
move is processed, and the adjudicating worker reads the deadline. No amount
of reloading moves `turn_started_at`, so no amount of reloading bought a
single second of real time.

What was wrong was the **projection**. `ClockView` promises durations that
are true as of its own `server_time`, and the snapshot filled it with
`remaining` — true as of `turn_started_at` — beside a `server_time` of *now*.
A client counting down from that, correctly and per its own contract, showed
the elapsed turn time as though it were still available. The live move path
happened to be right for a reason it did not state: there `at` *is* the new
`turn_started_at`, so the two agreed by coincidence.

These pin the distinction the projection needs and the invariant it owes.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.engine import PlayerSide
from app.modules.game.application.services.live_move_service import (
    _clock_view as live_clock_view,
)
from app.modules.game.application.services.match_snapshot_service import (
    _clock_view as snapshot_clock_view,
)
from app.modules.game.domain.clock import ClockState

TURN_STARTED = datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)


#: Thirty minutes each, LIGHT to move, the turn beginning at `TURN_STARTED`.
def _clock(
    *,
    light_ms: int = 1_800_000,
    dark_ms: int = 1_800_000,
    active: PlayerSide = PlayerSide.LIGHT,
) -> ClockState:
    return ClockState(
        light_ms=light_ms,
        dark_ms=dark_ms,
        active_side=active,
        turn_started_at=TURN_STARTED,
    )


class TestRemainingAt:
    def test_is_the_stored_value_when_the_turn_has_just_begun(self) -> None:
        clock = _clock()
        assert clock.remaining_at(PlayerSide.LIGHT, at=TURN_STARTED) == 1_800_000

    def test_deducts_the_time_the_active_side_has_been_thinking(self) -> None:
        clock = _clock()
        later = TURN_STARTED + timedelta(minutes=1)

        # The whole bug in one assertion: `remaining` still answers the
        # stored figure here, which is why the two cannot be the same method.
        assert clock.remaining_at(PlayerSide.LIGHT, at=later) == 1_740_000
        assert clock.remaining(PlayerSide.LIGHT) == 1_800_000

    def test_leaves_the_waiting_side_untouched(self) -> None:
        # Only one clock runs. Deducting from the other would invent a charge
        # against a player who is not on the move.
        clock = _clock()
        later = TURN_STARTED + timedelta(minutes=1)
        assert clock.remaining_at(PlayerSide.DARK, at=later) == 1_800_000

    def test_floors_at_zero_rather_than_going_negative(self) -> None:
        clock = _clock(light_ms=5_000)
        assert clock.remaining_at(PlayerSide.LIGHT, at=TURN_STARTED + timedelta(minutes=1)) == 0

    def test_never_credits_an_instant_before_the_turn_began(self) -> None:
        # Clock skew between two processes, not a player who moved early.
        clock = _clock()
        earlier = TURN_STARTED - timedelta(seconds=30)
        assert clock.remaining_at(PlayerSide.LIGHT, at=earlier) == 1_800_000


class TestTheProjectionIsSelfConsistent:
    """`server_time` and the durations must describe the same instant."""

    @pytest.mark.parametrize(
        "project", [snapshot_clock_view, live_clock_view], ids=["snapshot", "live"]
    )
    @pytest.mark.parametrize("idle_seconds", [0, 65, 600], ids=["fresh", "a-minute", "ten-minutes"])
    def test_the_deadline_is_server_time_plus_what_is_left(
        self, project: object, idle_seconds: int
    ) -> None:
        clock = _clock()
        at = TURN_STARTED + timedelta(seconds=idle_seconds)

        view = project(clock, at=at)  # type: ignore[operator]
        assert view is not None

        # The invariant a countdown depends on. A client renders
        # `light_ms - (now - server_time)`, so if this does not hold the
        # number on screen disagrees with the instant the server will flag.
        assert view.server_time + timedelta(milliseconds=view.light_ms) == view.deadline
        assert view.deadline == clock.deadline()

    def test_a_reload_does_not_hand_back_the_elapsed_turn(self) -> None:
        clock = _clock()

        # The reported reproduction: read the clock, wait, read it again.
        first = snapshot_clock_view(clock, at=TURN_STARTED + timedelta(seconds=55))
        second = snapshot_clock_view(clock, at=TURN_STARTED + timedelta(seconds=115))
        assert first is not None and second is not None

        # Before the fix both read 1_800_000 and the minute was returned.
        assert second.light_ms == first.light_ms - 60_000
        assert second.dark_ms == first.dark_ms
        # And neither reload moved the instant the server will flag at.
        assert first.deadline == second.deadline

    def test_the_live_path_is_unchanged_by_this(self) -> None:
        # `charged` makes the mover's `received_at` the new `turn_started_at`,
        # so on that path `at` is the turn's own start and nothing elapses.
        # The frame a moving client receives is byte-identical to before.
        clock = _clock()
        view = live_clock_view(clock, at=TURN_STARTED)
        assert view is not None
        assert view.light_ms == clock.light_ms
        assert view.dark_ms == clock.dark_ms

    def test_an_untimed_match_projects_nothing(self) -> None:
        assert snapshot_clock_view(None, at=TURN_STARTED) is None
        assert live_clock_view(None, at=TURN_STARTED) is None
