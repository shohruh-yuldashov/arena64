"""The durable notification, end to end — A64-021.1 §32.

Five claims, and every one of them needs a real database and the real
application graph:

  **A notification is produced by a source event, through the production
  wiring.** The pipeline under test is the whole of it — an HTTP friend
  request, an outbox row, the relay, the dispatcher's audience resolution
  and privacy gate, and `build_durable_notification_writer`, which is the
  factory `app_factory` itself calls. §33 asks for a reachability proof that
  a hand-built service cannot give, and this is it: the sink is composed
  exactly as the composition root composes it, so a test that passes here is
  a test the deployed worker path passes.

  **Exactly-once is structural.** The relay is drained twice over the same
  events, which is what a redelivery, a restart and two concurrent consumers
  all look like from the table's point of view. One row survives, because
  `(recipient_id, source_event_id, type)` is a constraint rather than a
  check somebody remembered to write.

  **Ownership is not a filter that can be forgotten.** Another player's
  notification is invisible to a list and produces a `404` — the same `404`
  an id that was never issued produces.

  **Keyset pagination is exact.** Every notification appears once across
  pages, in order, with no duplicate and no gap.

  **The badge follows the writes.** Mark one, mark all, and the count says
  what happened.

The relay is driven explicitly rather than by the timer: `OutboxWorker.stop`
and a `poll_interval` would make this suite depend on wall-clock sleeping,
which CLAUDE.md §6.4 rules out. `run_once` is public for exactly this.

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import OutboxSettings, get_settings
from app.core.clock import SystemClock
from app.core.identifiers import generate_uuid7
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.friends.infrastructure.cache import NoSocialGraphCache
from app.modules.notifications.domain.record import (
    ActorSummary,
    NavigationTarget,
    NavigationTargetType,
    NotificationAnnouncement,
    NotificationCategory,
    NotificationRecord,
    NotificationType,
)
from app.modules.notifications.infrastructure import (
    CompositeNotificationSink,
    LoggingNotificationSink,
    SqlAlchemyNotificationRepository,
)
from app.modules.notifications.presentation.dependencies import (
    build_durable_notification_writer,
    build_social_notification_dispatcher,
)
from app.modules.profiles.presentation.dependencies import build_profile_renderer
from app.platform.outbox import (
    OutboxRelay,
    SqlAlchemyOutboxRepository,
    SqlAlchemyProcessedEventStore,
)
from tests.contract.contract_app import build_contract_app, contract_client

NOTIFICATIONS_URL = "/api/v1/notifications"
UNREAD_URL = f"{NOTIFICATIONS_URL}/unread-count"
READ_ALL_URL = f"{NOTIFICATIONS_URL}/read-all"
REQUESTS_URL = "/api/v1/friends/requests"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"


class Player:
    def __init__(self, player_id: UUID, username: str, auth: dict[str, str]) -> None:
        self.id = player_id
        self.username = username
        self.auth = auth


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The production app, with the **real** event publisher."""
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


async def register(client: AsyncClient, session: AsyncSession) -> Player:
    suffix = uuid4().hex[:8]
    username = f"player{suffix}"
    assert len(username) <= 20, f"test username {username!r} exceeds the platform limit"

    created = await client.post(
        REGISTER_URL,
        json={"username": username, "email": f"{suffix}@example.com", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text

    # **Verified, because A64-021.5H made every outward-facing write require
    # it** — and a friend request is the source event this whole suite is
    # built on. Written directly rather than through the OTP flow: the code
    # is not this suite's subject, and reading it out of a log to type it
    # back would couple every notification test to the verification one.
    #
    # This is the same thing `app.operator.accounts verify` does, and its
    # absence is why these suites went red on A64-021.5H — that phase's
    # focused regression did not include this file.
    await session.execute(
        text("UPDATE users.user SET is_verified = true WHERE id = :id"),
        {"id": UUID(created.json()["data"]["id"])},
    )

    signed_in = await client.post(
        LOGIN_URL, json={"email": f"{suffix}@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return Player(
        UUID(created.json()["data"]["id"]),
        username,
        {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"},
    )


class RecordingAnnouncer:
    """A `NotificationAnnouncer` that keeps what it was handed — A64-021.2.

    It also **reads the database back** at announce time, which is the whole
    point: §5 and A64-021.1 §13 require the row to be durable before the
    frame is published, and the only way to assert an ordering is to observe
    it from inside. A writer that announced before committing would find the
    row absent here.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.announced: list[NotificationAnnouncement] = []
        self.visible_when_announced: list[bool] = []

    async def announce(self, announcements: Sequence[NotificationAnnouncement]) -> None:
        for announcement in announcements:
            page = await SqlAlchemyNotificationRepository(self._session).list_for(
                announcement.recipient_id, after=None, limit=50
            )
            self.visible_when_announced.append(
                any(record.id == announcement.notification_id for record in page.entries)
            )
        self.announced.extend(announcements)


async def drain(session: AsyncSession, announcer: Any | None = None) -> Any:
    """One relay tick, with the sink graph `app_factory` builds.

    `CompositeNotificationSink([durable, logging])` is not assembled by hand
    here in the sense that matters: `build_durable_notification_writer` is
    the composition root's own factory, and the order is the one it uses. A
    test that constructed a `DurableNotificationWriter` directly would prove
    the class works and nothing about whether the worker reaches it (§33).

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
        sink=CompositeNotificationSink(
            [
                build_durable_notification_writer(session, announcer=announcer),
                LoggingNotificationSink(),
            ]
        ),
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
    """Redis pools whose only consumer here is presence, which is disabled."""

    class _Pools:
        cache = None

    return _Pools()


async def seed(
    session: AsyncSession, recipient_id: UUID, *, count: int, actor: str = "someone"
) -> list[UUID]:
    """`count` notifications for one recipient, one second apart, oldest first.

    Written through the repository rather than through the pipeline because
    the subject here is *paging*, and staging six friend requests would test
    six registrations. The instants are distinct so the expected order is a
    fact rather than a tie the test would have to tolerate.
    """
    repository = SqlAlchemyNotificationRepository(session)
    base = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    written: list[UUID] = []
    for index in range(count):
        record = NotificationRecord(
            id=generate_uuid7(),
            recipient_id=recipient_id,
            type=NotificationType.FRIEND_REQUEST_RECEIVED,
            category=NotificationCategory.SOCIAL,
            payload=ActorSummary(
                player_id=uuid4(),
                username=f"{actor}{index}",
                display_name=None,
                avatar_object_key=None,
                avatar_version=0,
            ),
            target=NavigationTarget(type=NavigationTargetType.FRIEND_REQUESTS),
            source_event_id=uuid4(),
            created_at=base + timedelta(seconds=index),
        )
        assert await repository.append(record)
        written.append(record.id)
    await session.flush()
    return written


class TestNotificationsAreProducedBySourceEvents:
    async def test_a_sent_and_accepted_request_each_produce_one_durable_notification(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The whole pipeline, and the reachability proof — §32.1, §33.

        Two facts, two recipients, and neither actor is told what they just
        did: the addressee learns a request arrived, and the requester learns
        it was accepted.
        """
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )

        sent = await client.post(REQUESTS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        assert sent.status_code == 201, sent.text
        accepted = await client.post(
            f"{REQUESTS_URL}/{sent.json()['data']['id']}/accept", headers=bob.auth
        )
        assert accepted.status_code == 200, accepted.text

        # Nothing is delivered during the request — the HTTP graph contains
        # no dispatcher and no sink at all.
        assert (await client.get(NOTIFICATIONS_URL, headers=bob.auth)).json()["data"][
            "entries"
        ] == []

        await drain(contract_session)

        # Bob was sent a request: he is told about Alice, and the target is
        # the list where he can answer it.
        for_bob = (await client.get(NOTIFICATIONS_URL, headers=bob.auth)).json()["data"]
        assert [entry["type"] for entry in for_bob["entries"]] == ["friend_request_received"]
        received = for_bob["entries"][0]
        assert received["category"] == "social"
        assert received["actor"]["username"] == alice.username
        assert received["actor"]["player_id"] == str(alice.id)
        assert received["target"] == {"type": "friend_requests", "ref": None}
        assert received["is_read"] is False
        # §16: the outbox row that caused this is never published.
        assert "source_event_id" not in received

        # Alice's request was accepted: she is told about Bob, and the target
        # is his profile, by the username her client routes on.
        for_alice = (await client.get(NOTIFICATIONS_URL, headers=alice.auth)).json()["data"]
        assert [entry["type"] for entry in for_alice["entries"]] == ["friend_request_accepted"]
        assert for_alice["entries"][0]["target"] == {
            "type": "player_profile",
            "ref": bob.username,
        }

    async def test_redelivering_the_same_events_produces_no_second_row(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§32.2. Two drains over the same events, one row each.

        A second drain is what a redelivery, a relay restart and two
        concurrent consumer processes all look like from this table. The
        constraint refuses the duplicate; nothing here reads first.
        """
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        sent = await client.post(REQUESTS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        await client.post(f"{REQUESTS_URL}/{sent.json()['data']['id']}/accept", headers=bob.auth)

        await drain(contract_session)

        repository = SqlAlchemyNotificationRepository(contract_session)
        before = (await repository.list_for(bob.id, after=None, limit=50)).entries
        assert len(before) == 1

        # **The ledger.** A second tick finds the events already published
        # and delivers nothing — the first of the two defences.
        await drain(contract_session)
        assert len((await repository.list_for(bob.id, after=None, limit=50)).entries) == 1

        # **The constraint.** The second defence, and the one that survives a
        # crash between the write and the ledger: the same notification is
        # offered again with a fresh id, exactly as a redelivered event would
        # offer it, and the database refuses it without anybody reading first.
        duplicate = NotificationRecord(
            id=generate_uuid7(),
            recipient_id=before[0].recipient_id,
            type=before[0].type,
            category=before[0].category,
            payload=before[0].payload,
            target=before[0].target,
            source_event_id=before[0].source_event_id,
            created_at=before[0].created_at,
        )
        assert await repository.append(duplicate) is False

        after = (await repository.list_for(bob.id, after=None, limit=50)).entries
        assert [record.id for record in after] == [before[0].id]


class TestRealtimeAnnouncement:
    async def test_it_announces_after_the_commit_and_only_what_it_wrote(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """A64-021.2 §1, §5 — the realtime half of the write, in order.

        Three properties, and each is a rule the frame's harmlessness rests
        on:

        **After the commit.** The recording announcer reads the row back as
        it is announced, and finds it. A writer that announced first would
        wake a client that then read `GET /notifications` and found nothing
        — the exact race §5's "HTTP is authoritative" would otherwise lose.

        **Only what it wrote.** Two events, two recipients, two
        announcements — each addressed to the player whose notification it
        is, never to the actor.

        **A redelivery announces nothing.** The second drain writes no row,
        so it publishes no frame: a client that reconnects while the relay
        is catching up is not told the same thing twice by the server. The
        client's own duplicate guard is a second line, not the only one.
        """
        alice, bob = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        announcer = RecordingAnnouncer(contract_session)

        sent = await client.post(REQUESTS_URL, headers=alice.auth, json={"player_id": str(bob.id)})
        await client.post(f"{REQUESTS_URL}/{sent.json()['data']['id']}/accept", headers=bob.auth)

        await drain(contract_session, announcer)

        # Bob was told a request arrived; Alice was told hers was accepted.
        assert {(a.recipient_id, a.type) for a in announcer.announced} == {
            (bob.id, NotificationType.FRIEND_REQUEST_RECEIVED),
            (alice.id, NotificationType.FRIEND_REQUEST_ACCEPTED),
        }
        # Durable *before* announced, for every one of them.
        assert announcer.visible_when_announced == [True] * len(announcer.announced)

        # The announcement names the row that exists, not a fresh id.
        stored = await SqlAlchemyNotificationRepository(contract_session).list_for(
            bob.id, after=None, limit=50
        )
        announced_to_bob = next(a for a in announcer.announced if a.recipient_id == bob.id)
        assert announced_to_bob.notification_id == stored.entries[0].id
        assert announced_to_bob.created_at == stored.entries[0].created_at

        before = len(announcer.announced)
        await drain(contract_session, announcer)
        assert len(announcer.announced) == before


class TestOwnership:
    async def test_another_player_can_neither_see_nor_mark_a_notification(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§32.3, §30. A notification belongs to one recipient and to nobody
        else — and the refusal is indistinguishable from "no such thing"."""
        owner, stranger = (
            await register(client, contract_session),
            await register(client, contract_session),
        )
        (notification_id,) = await seed(contract_session, owner.id, count=1)

        assert (await client.get(NOTIFICATIONS_URL, headers=stranger.auth)).json()["data"][
            "entries"
        ] == []

        refused = await client.post(
            f"{NOTIFICATIONS_URL}/{notification_id}/read", headers=stranger.auth
        )
        assert refused.status_code == 404, refused.text

        # The same answer an id that was never issued gets. A `403` for the
        # first would confirm the notification exists.
        invented = await client.post(f"{NOTIFICATIONS_URL}/{uuid4()}/read", headers=stranger.auth)
        assert invented.status_code == refused.status_code
        assert invented.json()["code"] == refused.json()["code"]

        # And the owner's copy is untouched by the attempt.
        assert (await client.get(UNREAD_URL, headers=owner.auth)).json()["data"][
            "unread_count"
        ] == 1


class TestPaging:
    async def test_every_notification_appears_exactly_once_across_pages(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§32.4. Newest first, no duplicate, no gap, and a last page that
        says so."""
        owner = await register(client, contract_session)
        written = await seed(contract_session, owner.id, count=5)
        newest_first = [str(identifier) for identifier in reversed(written)]

        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            query = f"?limit=2{f'&after={cursor}' if cursor else ''}"
            page = (await client.get(f"{NOTIFICATIONS_URL}{query}", headers=owner.auth)).json()[
                "data"
            ]
            seen.extend(entry["id"] for entry in page["entries"])
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert pages < 10, "the cursor chain did not terminate"

        assert seen == newest_first
        assert pages == 3

        # A cursor this API did not issue is a `422` with a stable code, not
        # a `500` and not an empty page. `422` because `InvalidCursor` is a
        # `ValidationError` and the platform maps that family there.
        malformed = await client.get(f"{NOTIFICATIONS_URL}?after=not-a-cursor", headers=owner.auth)
        assert malformed.status_code == 422, malformed.text
        assert malformed.json()["code"] == "invalid_cursor"


class TestReadState:
    async def test_the_unread_count_follows_mark_one_and_mark_all(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§32.5, §9. Marking is idempotent and the badge is consistent with
        the list at every step."""
        owner = await register(client, contract_session)
        written = await seed(contract_session, owner.id, count=3)

        assert (await client.get(UNREAD_URL, headers=owner.auth)).json()["data"][
            "unread_count"
        ] == 3

        marked = await client.post(f"{NOTIFICATIONS_URL}/{written[0]}/read", headers=owner.auth)
        assert marked.status_code == 200, marked.text
        assert marked.json()["data"]["marked_read"] == 1
        assert (await client.get(UNREAD_URL, headers=owner.auth)).json()["data"][
            "unread_count"
        ] == 2

        # Idempotent: the second call succeeds, changes nothing, and says so.
        again = await client.post(f"{NOTIFICATIONS_URL}/{written[0]}/read", headers=owner.auth)
        assert again.status_code == 200
        assert again.json()["data"]["marked_read"] == 0
        assert (await client.get(UNREAD_URL, headers=owner.auth)).json()["data"][
            "unread_count"
        ] == 2

        all_read = await client.post(READ_ALL_URL, headers=owner.auth)
        assert all_read.json()["data"]["marked_read"] == 2
        assert (await client.get(UNREAD_URL, headers=owner.auth)).json()["data"][
            "unread_count"
        ] == 0

        # Nothing was deleted: read state and existence are different things.
        listed = (await client.get(NOTIFICATIONS_URL, headers=owner.auth)).json()["data"]
        assert len(listed["entries"]) == 3
        assert all(entry["is_read"] for entry in listed["entries"])

        # And a second mark-all is a successful no-op rather than an error.
        assert (await client.post(READ_ALL_URL, headers=owner.auth)).json()["data"][
            "marked_read"
        ] == 0
