"""Every registered handler answers the protocol it was registered under —
A64-028.4 §18, §19.

## The defect this exists for

`AnalyticsRetentionTask` is a `TaskHandler`: it has `name` and `run`, which
is what `PeriodicTaskScheduler` dispatches through. It was appended to
`build_outbox_worker`'s handler list, which holds `EventHandler`s and needs
`consumer`, `handles` and `handle`.

The relay called `handles()` on it **on every tick**, raised
`AttributeError`, and failed the whole pass — not one consumer, the pass.
`_dispatch` builds its work list in a comprehension, so the exception
escapes before any consumer runs. Nothing `platform.outbox` carries was
delivered by a process running that code: notifications, rating
application, analytics projections, tournament and social events.

And because it was never registered with the dispatcher either, the
six-hourly `analytics_prune_request` had no handler to route to — so the
400-day retention it exists for had never run once.

One misplaced `append`, two silent failures, and two layers of type
suppression over it: `list[TaskHandler | object]` on the annotation and
`# type: ignore[arg-type]` at the call site.

## What this file tests

Not the annotation — mypy owns that now. These test the two things a type
checker cannot see: that the *real* composition root produces a valid
registry, and that an invalid one is refused where somebody would notice.
"""

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.app_factory import build_outbox_worker, build_task_schedulers
from app.config.settings import Settings, get_settings
from app.database.redis import RedisPools
from app.database.session_manager import DatabaseSessionManager
from app.modules.analytics.application.services.retention_task import AnalyticsRetentionTask
from app.platform.outbox.relay import OutboxRelay
from app.platform.tasks import InlineTaskDispatcher, PeriodicTaskScheduler
from app.platform.tasks.ports import TaskRequest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def wiring(
    contract_engine: AsyncEngine, contract_redis: Redis
) -> tuple[DatabaseSessionManager, RedisPools, Settings]:
    """The real composition root's inputs, against real infrastructure.

    Nothing here is a fake: the builders under test are the ones `lifespan`
    calls, and a fake would only prove that a fake composes.
    """
    settings = get_settings()
    pools = RedisPools(
        live=contract_redis,
        bus=contract_redis,
        broker=contract_redis,
        cache=contract_redis,
        limits=contract_redis,
    )
    return DatabaseSessionManager(settings.postgres), pools, settings


class TestTheRealCompositionIsValid:
    async def test_the_relay_composes(
        self, wiring: tuple[DatabaseSessionManager, RedisPools, Settings]
    ) -> None:
        """`OutboxRelay` now refuses a consumer missing the protocol, so
        this is the whole assertion: the application's own handler list is
        one the relay accepts."""
        db, pools, settings = wiring

        assert build_outbox_worker(db, pools, settings) is not None

    async def test_every_scheduled_task_has_a_handler(
        self, wiring: tuple[DatabaseSessionManager, RedisPools, Settings]
    ) -> None:
        """`PeriodicTaskScheduler` refuses a request its dispatcher cannot
        route, so building the real set is the assertion. Before the fix
        this raised on `analytics.retention.prune`."""
        db, pools, settings = wiring

        assert build_task_schedulers(db, pools, settings)


class TestAnInvalidRegistrationIsRefused:
    def test_a_task_handler_cannot_be_an_outbox_consumer(self) -> None:
        """The exact mistake, at the exact place it was made.

        `AnalyticsRetentionTask` needs a session factory and a clock to
        construct, so the check is made against something with the same
        shape — a name and a run, and neither `handles` nor `handle`. What
        matters is the shape, which is what the relay actually inspects.
        """

        class TaskShaped:
            name = "analytics.retention.prune"

            async def run(self, payload: object) -> None: ...

        with pytest.raises(TypeError, match="handles"):
            OutboxRelay(
                outbox=object(),  # type: ignore[arg-type]
                processed=object(),  # type: ignore[arg-type]
                handlers=[TaskShaped()],  # type: ignore[list-item]
                unit_of_work=object(),  # type: ignore[arg-type]
                clock=object(),  # type: ignore[arg-type]
                worker_id="test",
                batch_size=1,
                max_attempts=1,
                retry_base_seconds=1,
                retry_max_seconds=1,
            )

    def test_the_error_names_what_is_missing(self) -> None:
        class Nameless:
            pass

        with pytest.raises(TypeError) as raised:
            OutboxRelay(
                outbox=object(),  # type: ignore[arg-type]
                processed=object(),  # type: ignore[arg-type]
                handlers=[Nameless()],  # type: ignore[list-item]
                unit_of_work=object(),  # type: ignore[arg-type]
                clock=object(),  # type: ignore[arg-type]
                worker_id="test",
                batch_size=1,
                max_attempts=1,
                retry_base_seconds=1,
                retry_max_seconds=1,
            )

        message = str(raised.value)
        assert "Nameless" in message
        assert "consumer" in message and "handles" in message and "handle" in message

    def test_an_unroutable_schedule_is_refused(self) -> None:
        class Handler:
            name = "something.else"

            async def run(self, payload: object) -> None: ...

        with pytest.raises(ValueError, match="analytics.retention.prune"):
            PeriodicTaskScheduler(
                dispatcher=InlineTaskDispatcher([Handler()]),
                request=TaskRequest(name="analytics.retention.prune"),
                interval_seconds=1.0,
            )


class TestTheTwoProtocolsAreDistinct:
    def test_the_retention_task_is_a_task_handler_and_not_an_event_handler(self) -> None:
        # Not a tautology: it is what makes "registered in the wrong list" a
        # statement about the lists rather than about the class.
        assert hasattr(AnalyticsRetentionTask, "name")
        assert hasattr(AnalyticsRetentionTask, "run")
        assert not hasattr(AnalyticsRetentionTask, "handles")
        assert not hasattr(AnalyticsRetentionTask, "handle")
