"""`InlineTaskDispatcher` and `PeriodicTaskScheduler` — AD-17's seam,
A64-014.1.

What is asserted here is almost entirely what these two **refuse** to do,
because those are the properties a caller could otherwise come to depend on
and lose on the day the dispatcher becomes Celery's: no result, no
propagated handler failure, and no dispatch at startup.

The scheduler's loop is driven through `trigger_once` rather than by
waiting on a timer. A test that slept an interval would be slow, flaky and
would assert the same thing — CLAUDE.md testing rule 4.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from app.platform.tasks import (
    DEFAULT_QUEUE,
    InlineTaskDispatcher,
    PeriodicTaskScheduler,
    TaskRequest,
    UnknownTask,
)


class RecordingHandler:
    """A `TaskHandler` that remembers what it was asked to do."""

    def __init__(self, name: str = "test.work", *, fails: bool = False) -> None:
        self._name = name
        self._fails = fails
        self.payloads: list[Mapping[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    async def run(self, payload: Mapping[str, Any]) -> None:
        self.payloads.append(payload)
        if self._fails:
            raise RuntimeError("the work blew up")


class TestInlineDispatcher:
    async def test_a_dispatched_request_reaches_its_handler(self) -> None:
        handler = RecordingHandler()
        dispatcher = InlineTaskDispatcher([handler])

        await dispatcher.dispatch(TaskRequest(name="test.work", payload={"id": 7}))

        assert handler.payloads == [{"id": 7}]

    async def test_a_request_reaches_only_its_own_handler(self) -> None:
        wanted = RecordingHandler("test.wanted")
        other = RecordingHandler("test.other")
        dispatcher = InlineTaskDispatcher([wanted, other])

        await dispatcher.dispatch(TaskRequest(name="test.wanted"))

        assert len(wanted.payloads) == 1
        assert other.payloads == []

    async def test_a_failing_handler_does_not_raise(self) -> None:
        """Matches Celery's semantics rather than a function call's. A
        caller that could catch a handler's exception would be a caller
        whose behaviour changes on the day the work moves to a broker."""
        dispatcher = InlineTaskDispatcher([RecordingHandler(fails=True)])

        await dispatcher.dispatch(TaskRequest(name="test.work"))

    async def test_an_unroutable_name_raises(self) -> None:
        """A *wiring* defect rather than a transient one: the process was
        started without the handler, so every dispatch until somebody
        notices is silence. Raising is the only thing that makes it
        visible."""
        dispatcher = InlineTaskDispatcher([RecordingHandler("test.work")])

        with pytest.raises(UnknownTask):
            await dispatcher.dispatch(TaskRequest(name="test.missing"))

    def test_two_handlers_with_one_name_are_refused_at_construction(self) -> None:
        """DI-06's "abort before accepting traffic", applied to wiring: a
        duplicate registration must fail at startup rather than silently let
        the last one win."""
        with pytest.raises(ValueError, match="two handlers"):
            InlineTaskDispatcher([RecordingHandler("test.work"), RecordingHandler("test.work")])

    def test_the_registered_names_are_reportable(self) -> None:
        """ "Which tasks does this process handle" is the first question
        during an incident where something scheduled is not happening."""
        dispatcher = InlineTaskDispatcher([RecordingHandler("a"), RecordingHandler("b")])

        assert dispatcher.registered == frozenset({"a", "b"})

    def test_a_request_defaults_to_the_default_queue(self) -> None:
        assert TaskRequest(name="test.work").queue == DEFAULT_QUEUE


class TestScheduler:
    async def test_triggering_dispatches_the_request(self) -> None:
        handler = RecordingHandler()
        scheduler = PeriodicTaskScheduler(
            dispatcher=InlineTaskDispatcher([handler]),
            request=TaskRequest(name="test.work"),
            interval_seconds=60.0,
        )

        await scheduler.trigger_once()

        assert len(handler.payloads) == 1

    async def test_starting_dispatches_nothing_immediately(self) -> None:
        """**The wait leads.** A prune or a sweep that fired the instant a
        process came up would run on every replica of a rolling deploy at
        once, which is the thundering herd the interval exists to spread."""
        handler = RecordingHandler()
        scheduler = PeriodicTaskScheduler(
            dispatcher=InlineTaskDispatcher([handler]),
            request=TaskRequest(name="test.work"),
            interval_seconds=60.0,
        )

        await scheduler.start()
        # One event-loop turn: enough for the task to be scheduled and to
        # reach its first wait, and not enough for a 60s interval to elapse.
        await asyncio.sleep(0)
        await scheduler.stop()

        assert handler.payloads == []

    async def test_the_loop_ticks_and_stops(self) -> None:
        handler = RecordingHandler()
        scheduler = PeriodicTaskScheduler(
            dispatcher=InlineTaskDispatcher([handler]),
            request=TaskRequest(name="test.work"),
            interval_seconds=0.01,
        )

        await scheduler.start()
        # Long enough for several intervals; the assertion is "at least
        # one", never an exact count, because an exact count would be a
        # timing assertion and therefore a flaky test.
        await asyncio.sleep(0.05)
        await scheduler.stop()

        assert handler.payloads

    async def test_starting_twice_is_a_no_op(self) -> None:
        scheduler = PeriodicTaskScheduler(
            dispatcher=InlineTaskDispatcher([RecordingHandler()]),
            request=TaskRequest(name="test.work"),
            interval_seconds=60.0,
        )

        await scheduler.start()
        await scheduler.start()
        await scheduler.stop()

    async def test_stopping_without_starting_is_a_no_op(self) -> None:
        scheduler = PeriodicTaskScheduler(
            dispatcher=InlineTaskDispatcher([RecordingHandler()]),
            request=TaskRequest(name="test.work"),
            interval_seconds=60.0,
        )

        await scheduler.stop()

    async def test_the_loop_outlives_a_failing_dispatch(self) -> None:
        """The handler raises on every tick and the loop keeps running: a
        schedule that exited on the first failure would need a human to
        notice, and nothing about a job that quietly stops is visible until
        the thing it was preventing has happened.

        The failure is now a **handler that raises** rather than a task with
        nowhere to go. A64-028.4 made the second impossible to construct —
        an unroutable schedule fails at composition — so producing one here
        would test a state the platform can no longer reach.
        """
        handler = RecordingHandler("test.work", fails=True)
        scheduler = PeriodicTaskScheduler(
            dispatcher=InlineTaskDispatcher([handler]),
            request=TaskRequest(name="test.work"),
            interval_seconds=0.01,
        )

        await scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()

        assert len(handler.payloads) > 1, "the loop stopped after the first failure"

    def test_a_schedule_with_nowhere_to_go_is_refused_at_construction(self) -> None:
        """A64-028.4 §19, and the second half of the analytics defect.

        `analytics_prune_request` was scheduled while its handler sat in the
        outbox relay's list. Nothing joined the two until the interval
        elapsed — six hours — and then the only symptom was one log line per
        interval while the 400-day prune never ran.
        """
        with pytest.raises(ValueError, match="test.work"):
            PeriodicTaskScheduler(
                dispatcher=InlineTaskDispatcher([RecordingHandler("test.other")]),
                request=TaskRequest(name="test.work"),
                interval_seconds=0.01,
            )
