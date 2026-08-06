"""The notification pipeline end to end — A64-013.7.

Three claims that only the whole stack can settle:

  **No fan-out during the request.** A64-013.7: "notifications must be
  delivered asynchronously ... do not perform fan-out during HTTP requests."
  Asserted as a pair: after a real accept over HTTP the outbox row **exists**
  and the sink is **untouched** — and the sink stays untouched because
  nothing on the request path can reach it. The application graph contains no
  dispatcher and no sink at all; the only thing that constructs one is the
  relay. The assertion is therefore a regression guard on that arrangement
  rather than a claim about timing.

  **The worker delivers what the request queued.** The relay is then run by
  hand, against the same database, and the notification arrives.

  **Blocked players never receive one, even if they were reachable when the
  event was recorded.** The block is placed *between* the accept and the
  drain, which is the case "do NOT trust enqueue-time state" is about and the
  one no unit test can stage against real rows.

The relay is driven explicitly rather than by the timer: `OutboxWorker.stop`
and a `poll_interval` would make this suite depend on wall-clock sleeping,
which CLAUDE.md §6.4 rules out. `run_once` is public for exactly this.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import OutboxSettings, get_settings
from app.core.clock import SystemClock
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.infrastructure.cache import NoSocialGraphCache
from app.modules.notifications.domain.notification import (
    NotificationKind,
    SocialNotification,
)
from app.modules.notifications.presentation.dependencies import (
    build_social_notification_dispatcher,
)
from app.modules.profiles.presentation.dependencies import build_profile_renderer
from app.platform.outbox import (
    OutboxModel,
    OutboxRelay,
    SqlAlchemyOutboxRepository,
    SqlAlchemyProcessedEventStore,
)
from tests.contract.contract_app import build_contract_app, contract_client

BLOCKS_URL = "/api/v1/blocks"
FRIENDS_URL = "/api/v1/friends"
REQUESTS_URL = f"{FRIENDS_URL}/requests"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
LOGOUT_ALL_URL = "/api/v1/auth/logout-all"
PASSWORD = "CorrectHorse1!"

ACCEPTED = "friends.friend_request_accepted"
BLOCKED_EVENT = "friends.player_blocked"
UNBLOCKED_EVENT = "friends.player_unblocked"
REMOVED_EVENT = "friends.friend_removed"


class RecordingSink:
    """A `NotificationSink` that keeps what it was handed.

    The one place a test can observe delivery, which is the point: with no
    transport in A64-013.7, "delivered" *is* "reached the sink".
    """

    def __init__(self) -> None:
        self.delivered: list[SocialNotification] = []

    async def deliver(self, notifications: list[SocialNotification]) -> None:
        self.delivered.extend(notifications)


class Player:
    def __init__(self, player_id: UUID, username: str, auth: dict[str, str]) -> None:
        self.id = player_id
        self.username = username
        self.auth = auth


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app, with the **real** event publisher.

    `build_contract_app` overrides nothing about the outbox by default, so
    every write below stages a real row on the test's session — which is
    what makes "the accept queued an event" assertable without a worker.
    """
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


@pytest_asyncio.fixture
def sink() -> RecordingSink:
    return RecordingSink()


async def register(client: AsyncClient) -> Player:
    suffix = uuid4().hex[:8]
    username = f"player{suffix}"
    assert len(username) <= 20, f"test username {username!r} exceeds the platform limit"

    created = await client.post(
        REGISTER_URL,
        json={"username": username, "email": f"{suffix}@example.com", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text

    signed_in = await client.post(
        LOGIN_URL, json={"email": f"{suffix}@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return Player(
        UUID(created.json()["data"]["id"]),
        username,
        {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"},
    )


async def befriend(client: AsyncClient, a: Player, b: Player) -> None:
    sent = await client.post(REQUESTS_URL, headers=a.auth, json={"player_id": str(b.id)})
    assert sent.status_code == 201, sent.text
    accepted = await client.post(
        f"{REQUESTS_URL}/{sent.json()['data']['id']}/accept", headers=b.auth
    )
    assert accepted.status_code == 200, accepted.text


async def queued_event_types(session: AsyncSession) -> list[str]:
    """Every unpublished event on the test's transaction, oldest first."""
    rows = await session.scalars(
        select(OutboxModel)
        .where(OutboxModel.published_at.is_(None))
        .order_by(OutboxModel.occurred_at, OutboxModel.id)
    )
    return [row.event_type for row in rows]


async def drain(session: AsyncSession, sink: RecordingSink) -> Any:
    """Runs one relay tick over the test's session, with the real dispatcher.

    The same object graph the worker builds in `app_factory`, minus the
    session factory — this test's session *is* the connection, because the
    rows it is draining live in a transaction nothing else can see.

    `NoSocialGraphCache` rather than the Redis one: a contract suite must not
    need Redis, and the cache changes how a read is served rather than what
    it answers.
    """
    cache = NoSocialGraphCache()
    settings = get_settings()
    dispatcher = build_social_notification_dispatcher(
        session,
        cache=cache,
        profiles=build_profile_renderer(
            session,
            pools=_no_redis_pools(),
            settings=settings,
            cache=cache,
            clock=SystemClock(),
        ),
        sink=sink,  # type: ignore[arg-type]
    )
    relay = OutboxRelay(
        outbox=SqlAlchemyOutboxRepository(session),
        processed=SqlAlchemyProcessedEventStore(session),
        handlers=[dispatcher],
        unit_of_work=SessionUnitOfWork(session),
        clock=SystemClock(),
        worker_id="contract-test",
        batch_size=OutboxSettings().batch_size,
        max_attempts=OutboxSettings().max_attempts,
        retry_base_seconds=OutboxSettings().retry_base_seconds,
        retry_max_seconds=OutboxSettings().retry_max_seconds,
    )
    return await relay.run_once()


def _no_redis_pools() -> Any:
    """Redis pools whose only consumer here is presence, which is disabled.

    `build_profile_renderer` takes pools because `RedisPresenceProvider`
    needs one; `PRESENCE_ENABLED` is false across the suite
    (`tests/conftest.py` and `contract_app`), so the branch that would touch
    them is not taken. Passing `None` makes that explicit — a real pool here
    would be a dependency this suite does not have and does not need.
    """

    class _Pools:
        cache = None

    return _Pools()


class TestNoFanOutDuringTheRequest:
    async def test_accepting_a_request_queues_an_event_and_delivers_nothing(
        self, client: AsyncClient, contract_session: AsyncSession, sink: RecordingSink
    ) -> None:
        """The brief's central performance rule.

        The HTTP call returns with the friendship created and the event
        durable — and with **nothing delivered**, because the request path
        has no dispatcher and no sink in its dependency graph at all. Wiring
        one in would make this fail, which is what the assertion is for.
        """
        alice, bob = await register(client), await register(client)

        await befriend(client, alice, bob)

        assert ACCEPTED in await queued_event_types(contract_session)
        assert sink.delivered == []

    async def test_blocking_queues_an_event_and_delivers_nothing(
        self, client: AsyncClient, contract_session: AsyncSession, sink: RecordingSink
    ) -> None:
        alice, bob = await register(client), await register(client)

        blocked = await client.post(BLOCKS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        assert blocked.status_code == 201, blocked.text

        assert BLOCKED_EVENT in await queued_event_types(contract_session)
        assert sink.delivered == []

    async def test_unblocking_queues_an_event(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The trigger that is easiest to forget, because the endpoint is
        idempotent and returns `204` either way."""
        alice, bob = await register(client), await register(client)
        await client.post(BLOCKS_URL, headers=alice.auth, json={"player_id": str(bob.id)})

        lifted = await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)
        assert lifted.status_code == 204, lifted.text

        assert UNBLOCKED_EVENT in await queued_event_types(contract_session)

    async def test_an_idempotent_unblock_queues_nothing(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """Lifting a block that is not there succeeds and changes nothing —
        so it must not announce a change. The publish sits after the
        repository's `remove`, which raises when there was nothing to lift."""
        alice, bob = await register(client), await register(client)

        lifted = await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)
        assert lifted.status_code == 204, lifted.text

        assert UNBLOCKED_EVENT not in await queued_event_types(contract_session)

    async def test_removing_a_friend_queues_an_event(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        alice, bob = await register(client), await register(client)
        await befriend(client, alice, bob)

        removed = await client.delete(f"{FRIENDS_URL}/{bob.id}", headers=alice.auth)
        assert removed.status_code == 204, removed.text

        assert REMOVED_EVENT in await queued_event_types(contract_session)

    async def test_a_rejected_request_queues_nothing(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """AD-16's other half: the event is written in the *same transaction*
        as the state change, so a refused write leaves no event behind. A
        self-block never commits — and never announces."""
        alice = await register(client)

        refused = await client.post(
            BLOCKS_URL, headers=alice.auth, json={"player_id": str(alice.id)}
        )
        assert refused.status_code == 422, refused.text

        assert BLOCKED_EVENT not in await queued_event_types(contract_session)


class TestWorkerDelivery:
    async def test_the_relay_delivers_the_acceptance_to_the_requester(
        self, client: AsyncClient, contract_session: AsyncSession, sink: RecordingSink
    ) -> None:
        """Async delivery, end to end: the request queued it, the worker
        delivered it, and each participant is told the half they did not do.

        **Two notifications since A64-021.1**, because `befriend` now stages
        two events: sending a request tells the addressee, accepting it tells
        the requester. Neither actor is told what they themselves just did.
        """
        alice, bob = await register(client), await register(client)
        await befriend(client, alice, bob)

        await drain(contract_session, sink)

        assert {(n.recipient_id, n.kind) for n in sink.delivered} == {
            (bob.id, NotificationKind.FRIEND_REQUEST_RECEIVED),
            (alice.id, NotificationKind.FRIEND_REQUEST_ACCEPTED),
        }
        accepted = next(
            n for n in sink.delivered if n.kind is NotificationKind.FRIEND_REQUEST_ACCEPTED
        )
        assert accepted.subject.identity.id == bob.id

    async def test_a_drained_event_is_marked_published(
        self, client: AsyncClient, contract_session: AsyncSession, sink: RecordingSink
    ) -> None:
        alice, bob = await register(client), await register(client)
        await befriend(client, alice, bob)

        await drain(contract_session, sink)

        assert await queued_event_types(contract_session) == []

    async def test_draining_twice_delivers_once(
        self, client: AsyncClient, contract_session: AsyncSession, sink: RecordingSink
    ) -> None:
        """At-least-once delivery with an idempotent consumer. The second
        tick claims nothing, and even if it did the ledger would filter it."""
        alice, bob = await register(client), await register(client)
        await befriend(client, alice, bob)

        await drain(contract_session, sink)
        delivered_once = len(sink.delivered)
        await drain(contract_session, sink)

        # Two notifications per befriending since A64-021.1 — see the test
        # above. What this asserts is that the second tick adds none.
        assert delivered_once == 2
        assert len(sink.delivered) == delivered_once

    async def test_the_three_silent_events_deliver_nothing(
        self, client: AsyncClient, contract_session: AsyncSession, sink: RecordingSink
    ) -> None:
        """FS-2 and BL-1 through the whole stack: a removal, a block and an
        unblock are all recorded and none of them tells anybody."""
        alice, bob = await register(client), await register(client)
        await befriend(client, alice, bob)
        await client.delete(f"{FRIENDS_URL}/{bob.id}", headers=alice.auth)
        await client.post(BLOCKS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        await client.delete(f"{BLOCKS_URL}/{bob.id}", headers=alice.auth)

        await drain(contract_session, sink)

        # Only `befriend`'s two events produced notifications; the removal,
        # the block and the unblock produced none.
        assert sorted(n.kind for n in sink.delivered) == sorted(
            [
                NotificationKind.FRIEND_REQUEST_RECEIVED,
                NotificationKind.FRIEND_REQUEST_ACCEPTED,
            ]
        )
        assert await queued_event_types(contract_session) == []


class TestBlockedRecipients:
    async def test_a_block_placed_before_the_drain_suppresses_the_notification(
        self, client: AsyncClient, contract_session: AsyncSession, sink: RecordingSink
    ) -> None:
        """The rule that requires re-reading, staged against real rows.

        Alice's request is accepted — so at enqueue time she is reachable and
        the payload names her. Bob then blocks her. The relay runs, re-reads
        the block set, and delivers nothing.
        """
        alice, bob = await register(client), await register(client)
        await befriend(client, alice, bob)

        blocked = await client.post(BLOCKS_URL, headers=bob.auth, json={"player_id": str(alice.id)})
        assert blocked.status_code == 201, blocked.text

        await drain(contract_session, sink)

        assert sink.delivered == []

    async def test_the_same_event_is_delivered_when_no_block_intervenes(
        self, client: AsyncClient, contract_session: AsyncSession, sink: RecordingSink
    ) -> None:
        """The control for the test above: the suppression must be the
        block, not the re-read."""
        alice, bob = await register(client), await register(client)
        await befriend(client, alice, bob)

        await drain(contract_session, sink)

        assert {n.recipient_id for n in sink.delivered} == {alice.id, bob.id}

    async def test_a_blocked_pair_produces_no_notification_in_either_direction(
        self, client: AsyncClient, contract_session: AsyncSession, sink: RecordingSink
    ) -> None:
        """`blocked_ids_for` is symmetric, so it does not matter which of the
        two placed the block — a blocked player learns nothing either way."""
        alice, bob = await register(client), await register(client)
        await befriend(client, alice, bob)
        await client.post(BLOCKS_URL, headers=alice.auth, json={"player_id": str(bob.id)})

        await drain(contract_session, sink)

        assert sink.delivered == []
