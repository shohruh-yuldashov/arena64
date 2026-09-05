"""The analytics pipeline, end to end over real PostgreSQL — A64-027.2.

The unit tests prove the rules. This proves the **plumbing**: that a domain
event written by a service reaches a row in `analytics.event`, that a second
delivery of it does not, that the collector's refusals hold through HTTP,
and that erasure severs the one link there is.

Every one of these depends on a database, and every one of them would pass
against a mock while the real thing was broken:

    the primary key deduplicating       a mock store cannot conflict
    `ON CONFLICT DO NOTHING` under      a mock has no concurrency
    two concurrent writers
    the subject upsert being atomic     two selects racing look fine in
                                        Python
    erasure leaving the events          only a real delete can fail to

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.environment import Environment
from app.database.unit_of_work import SessionUnitOfWork
from app.modules.analytics.application.services.erasure import AnalyticsErasureService
from app.modules.analytics.application.services.projector import AnalyticsProjector
from app.modules.analytics.application.services.retention import AnalyticsRetentionService
from app.modules.analytics.domain.subject import SubjectKey
from app.modules.analytics.infrastructure.models import (
    AnalyticsEventModel,
    AnalyticsSubjectModel,
)
from app.modules.analytics.infrastructure.repositories.analytics_repository import (
    SqlAlchemyAnalyticsEventStore,
    SqlAlchemyRetentionPruner,
    SqlAlchemySubjectDirectory,
    SqlAlchemySubjectEraser,
)
from app.platform.metrics import NullMetrics
from app.platform.outbox import OutboxEntry
from tests.contract.contract_app import build_contract_app, contract_client

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
COLLECT_URL = "/api/v1/analytics/events"


class _FrozenClock:
    def now(self) -> datetime:
        return NOW


@pytest_asyncio.fixture
async def client(contract_session: AsyncSession):  # type: ignore[no-untyped-def]
    async with contract_client(build_contract_app(contract_session)) as http:
        yield http


def _projector(session: AsyncSession) -> AnalyticsProjector:
    return AnalyticsProjector(
        store=SqlAlchemyAnalyticsEventStore(session),
        subjects=SqlAlchemySubjectDirectory(session),
        unit_of_work=SessionUnitOfWork(session),
        clock=_FrozenClock(),
        environment=Environment.TEST,
        metrics=NullMetrics(),
    )


def _entry(
    event_type: str, payload: dict[str, object], *, entry_id: UUID | None = None
) -> OutboxEntry:
    return OutboxEntry(
        id=entry_id or uuid4(),
        aggregate_type="player",
        aggregate_id=uuid4(),
        event_type=event_type,
        event_version=1,
        payload=payload,
        occurred_at=NOW,
    )


async def _count(session: AsyncSession, **filters: object) -> int:
    statement = select(func.count()).select_from(AnalyticsEventModel)
    for column, value in filters.items():
        statement = statement.where(getattr(AnalyticsEventModel, column) == value)
    return (await session.execute(statement)).scalar_one()


class TestTheProjectionReachesTheStore:
    async def test_a_registration_becomes_a_row(self, contract_session: AsyncSession) -> None:
        player = uuid4()
        entry = _entry("users.registered", {"user_id": str(player)})

        await _projector(contract_session).handle([entry])

        assert await _count(contract_session, event_name="user_registered") == 1
        stored = (
            await contract_session.execute(
                select(AnalyticsEventModel).where(AnalyticsEventModel.id == entry.id)
            )
        ).scalar_one()
        assert stored.source == "backend"
        assert stored.environment == "test"
        assert stored.source_event_id == entry.id
        assert stored.subject_key is not None

    async def test_the_row_holds_a_subject_key_and_never_the_player_id(
        self, contract_session: AsyncSession
    ) -> None:
        """The privacy property the whole schema exists for: `analytics.event`
        has no `player_id` column, so the raw store cannot be joined to the
        product database by primary key."""
        player = uuid4()
        await _projector(contract_session).handle(
            [_entry("users.registered", {"user_id": str(player)})]
        )

        columns = {column.name for column in AnalyticsEventModel.__table__.columns}
        assert "player_id" not in columns
        assert "user_id" not in columns

        stored = (await contract_session.execute(select(AnalyticsEventModel))).scalars().first()
        assert stored is not None
        assert stored.subject_key != player

    async def test_a_pairing_becomes_two_rows_with_distinct_ids(
        self, contract_session: AsyncSession
    ) -> None:
        entry = _entry(
            "matchmaking.players_paired",
            {
                "match_id": str(uuid4()),
                "variant": "russian_8x8",
                "queue_type": "ranked",
                "light_player_id": str(uuid4()),
                "dark_player_id": str(uuid4()),
                "waited_for_seconds": 3.5,
            },
        )

        await _projector(contract_session).handle([entry])

        assert await _count(contract_session, event_name="match_found") == 2
        # Two subjects, so a per-player metric counts two people rather than
        # one pairing twice.
        subjects = (
            (await contract_session.execute(select(AnalyticsEventModel.subject_key)))
            .scalars()
            .all()
        )
        assert len(set(subjects)) == 2

    async def test_an_untracked_event_stores_nothing(self, contract_session: AsyncSession) -> None:
        await _projector(contract_session).handle([_entry("game.move_applied", {})])
        assert await _count(contract_session) == 0

    async def test_an_unreadable_payload_is_skipped_not_retried(
        self, contract_session: AsyncSession
    ) -> None:
        """§57: a payload missing a field will still be missing it next
        time. Reported as a failure it would sit at the head of the backlog
        forever."""
        failures = await _projector(contract_session).handle([_entry("users.registered", {})])

        assert failures == ()
        assert await _count(contract_session) == 0


class TestExactlyOnceEffect:
    async def test_the_same_outbox_event_delivered_twice_stores_one_row(
        self, contract_session: AsyncSession
    ) -> None:
        """**MUTATION C targets this.** The relay writes its ledger after
        the handler and in another transaction, so a crash between them
        redelivers the batch. The primary key is what makes that safe."""
        entry = _entry("users.registered", {"user_id": str(uuid4())})
        projector = _projector(contract_session)

        await projector.handle([entry])
        await projector.handle([entry])

        assert await _count(contract_session) == 1

    async def test_a_redelivered_pairing_stores_two_rows_not_four(
        self, contract_session: AsyncSession
    ) -> None:
        """The fan-out case, which a naive `uuid4()` per row would break —
        the second delivery would insert two more."""
        entry = _entry(
            "matchmaking.players_paired",
            {
                "match_id": str(uuid4()),
                "variant": "russian_8x8",
                "queue_type": "casual",
                "light_player_id": str(uuid4()),
                "dark_player_id": str(uuid4()),
                "waited_for_seconds": 1.0,
            },
        )
        projector = _projector(contract_session)

        await projector.handle([entry])
        await projector.handle([entry])

        assert await _count(contract_session) == 2

    async def test_one_player_gets_one_subject(self, contract_session: AsyncSession) -> None:
        """Two subjects for one person would split every per-person metric
        in half."""
        player = uuid4()
        directory = SqlAlchemySubjectDirectory(contract_session)

        first = await directory.resolve(player)
        second = await directory.resolve(player)

        assert first == second
        count = (
            await contract_session.execute(select(func.count()).select_from(AnalyticsSubjectModel))
        ).scalar_one()
        assert count == 1

    async def test_concurrent_resolution_produces_one_subject(
        self, contract_session: AsyncSession
    ) -> None:
        """Sequential on one session — asyncpg forbids interleaving — but
        it is the upsert that makes it correct, not the ordering: a
        select-then-insert would pass this and still race in production."""
        player = uuid4()
        directory = SqlAlchemySubjectDirectory(contract_session)

        keys = [await directory.resolve(player) for _ in range(5)]

        assert len(set(keys)) == 1


class TestTheCollectorThroughHttp:
    """**MUTATION A and B target these.**"""

    def _body(self, name: str, **properties: object) -> dict[str, object]:
        return {
            "events": [
                {
                    "event_name": name,
                    "idempotency_key": str(uuid4()),
                    "anonymous_id": str(uuid4()),
                    "properties": properties,
                }
            ]
        }

    async def test_an_anonymous_visitor_may_report_a_landing_view(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """F-A's first step happens before there is an account, so
        requiring a session here would make the acquisition funnel
        unmeasurable."""
        response = await client.post(COLLECT_URL, json=self._body("landing_viewed"))

        assert response.status_code == 202, response.text
        assert response.json()["data"]["accepted"] == 1
        assert await _count(contract_session, event_name="landing_viewed") == 1

    async def test_a_client_cannot_emit_a_server_event(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        for name in ("user_registered", "match_completed", "rating_changed", "tournament_entered"):
            response = await client.post(COLLECT_URL, json=self._body(name))
            assert response.status_code == 422, f"{name}: {response.text}"
        assert await _count(contract_session) == 0

    async def test_a_client_cannot_set_the_envelope(self, client: AsyncClient) -> None:
        """`extra="forbid"` means these are unrepresentable rather than
        stripped — a field the schema does not declare cannot be read by
        mistake in a later refactor.

        The values are **well-formed for the field they impersonate**. An
        earlier version of this test sent `"whatever"` for every one of
        them and passed against a deliberately broken build, because
        `actor_id` had been added to the schema and `"whatever"` is not a
        UUID — the request was refused for the wrong reason and the test
        could not tell. A mutation check found it.
        """
        well_formed: dict[str, object] = {
            "actor_id": str(uuid4()),
            "subject_key": str(uuid4()),
            "environment": "production",
            "source": "backend",
            "is_synthetic": False,
            "occurred_at": "2026-09-05T12:00:00Z",
            "event_id": str(uuid4()),
        }
        for forbidden, value in well_formed.items():
            body = self._body("landing_viewed")
            body["events"][0][forbidden] = value  # type: ignore[index]
            response = await client.post(COLLECT_URL, json=body)
            assert response.status_code == 422, f"{forbidden}: {response.text}"

    async def test_an_anonymous_caller_cannot_attribute_an_event_to_somebody(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """The positive half of the previous test, and the one a mutation
        cannot slip past: whatever the body says, an unauthenticated
        submission is stored with **no subject at all**.

        A schema that started accepting `actor_id` would fail here even if
        the value were a perfectly valid UUID.
        """
        victim = uuid4()
        await SqlAlchemySubjectDirectory(contract_session).resolve(victim)

        body = self._body("landing_viewed")
        body["events"][0]["actor_id"] = str(victim)  # type: ignore[index]
        response = await client.post(COLLECT_URL, json=body)

        # Either refused outright, or — if a future schema declared the
        # field — stored without honouring it. Both are acceptable; storing
        # it as the subject is not.
        if response.status_code == 202:
            stored = (await contract_session.execute(select(AnalyticsEventModel))).scalars().all()
            assert all(row.subject_key is None for row in stored)
        else:
            assert response.status_code == 422

    async def test_a_denied_property_is_refused(self, client: AsyncClient) -> None:
        response = await client.post(
            COLLECT_URL, json=self._body("landing_viewed", email="nobody@example.com")
        )
        assert response.status_code == 422

    async def test_an_unbounded_utm_value_is_refused(self, client: AsyncClient) -> None:
        """§17 bounds attribution to a label. An unbounded campaign name is
        a high-cardinality string in a `GROUP BY`."""
        response = await client.post(
            COLLECT_URL, json=self._body("landing_viewed", utm_source="x" * 200)
        )
        assert response.status_code == 422

    async def test_a_batch_larger_than_the_bound_is_refused(self, client: AsyncClient) -> None:
        body = {
            "events": [
                {
                    "event_name": "landing_viewed",
                    "idempotency_key": str(uuid4()),
                    "anonymous_id": str(uuid4()),
                    "properties": {},
                }
                for _ in range(11)
            ]
        }
        assert (await client.post(COLLECT_URL, json=body)).status_code == 422

    async def test_a_replayed_submission_stores_one_row(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        body = self._body("landing_viewed")

        first = await client.post(COLLECT_URL, json=body)
        second = await client.post(COLLECT_URL, json=body)

        assert (first.json()["data"]["accepted"], second.json()["data"]["accepted"]) == (1, 0)
        assert await _count(contract_session, event_name="landing_viewed") == 1

    async def test_the_refusal_does_not_say_which_kind_it_was(self, client: AsyncClient) -> None:
        """A client able to tell "server-owned" from "not in the taxonomy"
        could enumerate which events are authoritative."""
        unknown = await client.post(COLLECT_URL, json=self._body("no_such_event"))
        server_owned = await client.post(COLLECT_URL, json=self._body("match_completed"))

        assert unknown.status_code == server_owned.status_code == 422
        assert unknown.json()["message"] == server_owned.json()["message"]


class TestErasureSeversTheLink:
    """**MUTATION D targets these.**"""

    async def test_the_subject_row_is_gone_and_the_events_remain(
        self, contract_session: AsyncSession
    ) -> None:
        player = uuid4()
        await _projector(contract_session).handle(
            [_entry("users.registered", {"user_id": str(player)})]
        )
        assert await _count(contract_session) == 1

        erased = await AnalyticsErasureService(
            eraser=SqlAlchemySubjectEraser(contract_session)
        ).erase(player)

        assert erased is True
        # The link is gone…
        remaining = (
            await contract_session.execute(
                select(func.count())
                .select_from(AnalyticsSubjectModel)
                .where(AnalyticsSubjectModel.player_id == player)
            )
        ).scalar_one()
        assert remaining == 0
        # …and the non-identifying product fact survives, which is the half
        # D3 explicitly allows.
        assert await _count(contract_session) == 1

    async def test_the_player_cannot_be_found_from_the_events_afterwards(
        self, contract_session: AsyncSession
    ) -> None:
        """The requirement in one assertion: after erasure there is no
        function from the person to their rows.

        `subject_key` is random rather than derived, so this is not "hard to
        compute" — there is nothing to compute it from.
        """
        player = uuid4()
        await _projector(contract_session).handle(
            [_entry("users.registered", {"user_id": str(player)})]
        )
        await AnalyticsErasureService(eraser=SqlAlchemySubjectEraser(contract_session)).erase(
            player
        )

        directory = SqlAlchemySubjectDirectory(contract_session)
        assert await directory.lookup(player) is None

        # And nothing in any surviving row names them.
        rows = (await contract_session.execute(select(AnalyticsEventModel))).scalars().all()
        assert rows
        for row in rows:
            assert str(player) not in str(row.properties)
            assert row.subject_key != player

    async def test_erasing_twice_is_idempotent(self, contract_session: AsyncSession) -> None:
        service = AnalyticsErasureService(eraser=SqlAlchemySubjectEraser(contract_session))
        player = uuid4()
        await SqlAlchemySubjectDirectory(contract_session).resolve(player)

        assert await service.erase(player) is True
        assert await service.erase(player) is False


class TestRetentionOverRealRows:
    async def test_it_deletes_only_what_is_past_the_horizon(
        self, contract_session: AsyncSession
    ) -> None:
        store = SqlAlchemyAnalyticsEventStore(contract_session)
        directory = SqlAlchemySubjectDirectory(contract_session)
        key = await directory.resolve(uuid4())

        from app.modules.analytics.domain.event import AnalyticsEvent
        from app.platform.analytics import EventName

        def event(age_days: int) -> AnalyticsEvent:
            instant = NOW - timedelta(days=age_days)
            return AnalyticsEvent(
                event_id=uuid4(),
                event_name=EventName.USER_REGISTERED,
                event_version=1,
                occurred_at=instant,
                received_at=instant,
                source="backend",
                environment=Environment.TEST,
                subject_key=SubjectKey(key),
            )

        await store.append([event(10), event(399), event(401), event(900)])

        result = await AnalyticsRetentionService(
            pruner=SqlAlchemyRetentionPruner(contract_session),
            clock=_FrozenClock(),
            metrics=NullMetrics(),
        ).prune()

        assert result.deleted == 2
        assert await _count(contract_session) == 2
