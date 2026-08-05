"""Time controls end to end — real PostgreSQL, real constraints, the real
composition root (A64-020.5A-pre §23).

Five tests, and each asserts something that only the whole path can show.
The unit suites already cover the pieces — `test_queue_pool.py` for pool
identity, `test_pairing_service.py` for what the scan builds — so nothing
here re-checks a value object.

    the catalogue                the enum and the table are one catalogue
    joining                      a control that is not offered is refused
    recovery                     a reconnecting client is told what it chose
    the match                    the durable snapshot, the clock, the deadline
    the seat                     the ladder the result will move

The graph under test is the one that ships. Nothing about `matchmaking`,
`game` or `reference` is overridden — the real catalogue over the seeded
table, the real queue service, the real pairing scan, the real acceptance
service. The only double is the clock deadline store, because AD-21 puts
deadlines in Redis and `ASGITransport` has none (`contract_app.py`).

Skipped, not failed, when PostgreSQL is unreachable (see `conftest.py`).
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.core.clock import SystemClock
from app.modules.game.infrastructure import SqlAlchemyMatchRecordRepository
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.application.services import PairingOutcome
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType, Region
from app.modules.matchmaking.presentation.dependencies import (
    build_match_creation,
    build_pairing_exclusions,
    build_pairing_service,
    build_recent_opponents,
)
from app.modules.rating.public import SpeedClass
from app.modules.reference.infrastructure.repositories import SqlAlchemyTimeControlCatalogue
from app.modules.reference.public import TimeControlId
from app.platform.outbox import NoEventPublisher
from tests.contract.contract_app import build_contract_app, contract_client
from tests.fakes.clock_deadlines import RecordingClockDeadlines
from tests.fakes.metrics import RecordingMetrics
from tests.fakes.time_controls import SEEDED_TIME_CONTROLS

CATALOGUE_URL = "/api/v1/time-controls"
QUEUE_URL = "/api/v1/matchmaking/queue"
MY_QUEUE_URL = "/api/v1/matchmaking/queue/me"
PENDING_URL = "/api/v1/matchmaking/matches/pending"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PASSWORD = "CorrectHorse1!"

#: The control every test here queues for unless it is about a different
#: one. Named rather than inlined so a reader sees "3+2 blitz" once.
BLITZ = TimeControlId.BLITZ_3_2


def _accept_url(match_id: UUID) -> str:
    return f"/api/v1/matchmaking/matches/{match_id}/accept"


class Player:
    def __init__(self, player_id: UUID, auth: dict[str, str]) -> None:
        self.id = player_id
        self.auth = auth


async def register(client: AsyncClient) -> Player:
    suffix = uuid4().hex[:8]
    created = await client.post(
        REGISTER_URL,
        json={
            "username": f"player{suffix}",
            "email": f"{suffix}@example.com",
            "password": PASSWORD,
        },
    )
    assert created.status_code == 201, created.text

    signed_in = await client.post(
        LOGIN_URL, json={"email": f"{suffix}@example.com", "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    return Player(
        UUID(created.json()["data"]["id"]),
        {"Authorization": f"Bearer {signed_in.json()['data']['access_token']}"},
    )


@pytest_asyncio.fixture
async def deadlines() -> RecordingClockDeadlines:
    return RecordingClockDeadlines()


@pytest_asyncio.fixture
async def client(
    contract_session: AsyncSession, deadlines: RecordingClockDeadlines
) -> AsyncIterator[AsyncClient]:
    app = build_contract_app(contract_session, deadlines=deadlines)
    async with contract_client(app) as http:
        yield http


class TestTheCatalogue:
    async def test_it_offers_exactly_the_seeded_controls_in_display_order(
        self, contract_session: AsyncSession
    ) -> None:
        """The one assertion that holds three copies of this catalogue
        together — the `TimeControlId` enum, the seeded rows and the
        fixtures in `tests/fakes/time_controls.py`.

        The enum decides which controls exist and the table decides what
        each one is (`reference.domain.time_control`), which is only a
        sound split while the two agree about membership. Nothing in the
        type system checks that: a member added without a row is a `422` for
        anybody who picks it, and a row is what a migration writes.

        Order is asserted because it is a *contract*. A picker renders this
        list, and a player who learned that the second entry is 3+2 must not
        find something else there on another device.
        """
        catalogue = SqlAlchemyTimeControlCatalogue(contract_session)

        offered = await catalogue.active()

        assert [entry.id for entry in offered] == [
            snapshot.id for snapshot, _ in SEEDED_TIME_CONTROLS
        ]
        assert {entry.id for entry in offered} == set(TimeControlId)
        assert [
            (entry.base_time_ms, entry.increment_ms, entry.speed_class) for entry in offered
        ] == [
            (snapshot.base_time_ms, snapshot.increment_ms, snapshot.speed_class)
            for snapshot, _ in SEEDED_TIME_CONTROLS
        ]


class TestTheCatalogueOverHttp:
    async def test_it_publishes_what_a_picker_needs_and_nothing_else(
        self, client: AsyncClient
    ) -> None:
        """A64-020.5A §3 and §4. The endpoint exists so a lobby never has to
        hardcode the four controls or parse a duration out of an identifier
        — either would make the frontend a second definition of what "3+2"
        means, and the first one to drift would win silently.

        Three properties, and each is one a client depends on:

          - the **order** is the catalogue's, so a picker renders the same
            list in the same sequence on every device
          - every entry carries what a label needs, so rendering costs no
            second request
          - `is_active` is **absent**, which is the contract: only active
            controls are returned, and a field that is `true` on every row
            would invite a client to filter on something already filtered

        Authenticated, like every route outside `/health` — asserted here
        because "visible to every player" is not "reachable without a
        token", and an exception would be the first one on the platform.
        """
        anonymous = await client.get(CATALOGUE_URL)
        assert anonymous.status_code == 401, anonymous.text

        alice = await register(client)
        response = await client.get(CATALOGUE_URL, headers=alice.auth)

        assert response.status_code == 200, response.text
        offered = response.json()["data"]
        assert [entry["id"] for entry in offered] == [
            snapshot.id.value for snapshot, _ in SEEDED_TIME_CONTROLS
        ]
        assert offered[1] == {
            "id": "blitz_3_2",
            "label": "3+2",
            "base_time_ms": 180_000,
            "increment_ms": 2_000,
            "speed_class": "blitz",
        }

    async def test_an_identifier_it_publishes_is_one_the_queue_accepts(
        self, client: AsyncClient
    ) -> None:
        """The property that makes the catalogue *usable* rather than merely
        readable, and the one no single-endpoint test can see: every `id`
        this route publishes is accepted verbatim by `POST /queue`.

        A catalogue whose identifiers the queue refused would be worse than
        no catalogue — a client would render four options and every one of
        them would fail. Asserted by joining and leaving with each in turn,
        because QT-1 allows one live ticket at a time.
        """
        alice = await register(client)
        offered = (await client.get(CATALOGUE_URL, headers=alice.auth)).json()["data"]

        for entry in offered:
            joined = await client.post(
                QUEUE_URL,
                headers=alice.auth,
                json={"queue_type": "casual", "time_control_id": entry["id"]},
            )
            assert joined.status_code == 201, joined.text
            assert joined.json()["data"]["time_control_id"] == entry["id"]
            left = await client.delete(QUEUE_URL, headers=alice.auth)
            assert left.status_code == 204, left.text


class TestJoiningNamesAControl:
    async def test_a_control_the_platform_does_not_offer_is_refused(
        self, client: AsyncClient
    ) -> None:
        """A64-020.5A-pre §8 and §22. `deactivated_1_0` is not a
        `TimeControlId`, so this is the shape a stale client sends after a
        control is renamed — and the answer must name the *field* rather
        than failing somewhere inside the queue.

        `422`, because a body naming something the platform does not offer
        is a malformed request, not a rule the player broke.
        """
        alice = await register(client)

        response = await client.post(
            QUEUE_URL,
            headers=alice.auth,
            json={"queue_type": "ranked", "time_control_id": "deactivated_1_0"},
        )

        assert response.status_code == 422, response.text
        assert "time_control_id" in response.json()["message"]

    async def test_a_recovered_ticket_reports_the_control_it_was_entered_with(
        self, client: AsyncClient
    ) -> None:
        """§17. A client that refreshed the page has lost everything it
        knew; `GET /queue/me` is the whole of its memory, so the response
        has to carry enough to re-render the lobby — the identifier *and*
        what it means.

        Asserted through the read rather than the write, because the write
        could echo its own request body and prove nothing about what was
        stored.
        """
        alice = await register(client)
        joined = await client.post(
            QUEUE_URL,
            headers=alice.auth,
            json={"queue_type": "ranked", "time_control_id": "rapid_10_0"},
        )
        assert joined.status_code == 201, joined.text

        recovered = await client.get(MY_QUEUE_URL, headers=alice.auth)

        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"]["time_control_id"] == "rapid_10_0"
        assert recovered.json()["data"]["base_time_ms"] == 600_000
        assert recovered.json()["data"]["increment_ms"] == 0
        assert recovered.json()["data"]["speed_class"] == "rapid"


class TestAPairingCarriesTheControlThroughToTheClock:
    """§11 through §15, in one pass of the real machinery.

    Two players queue for 3+2, a real pairing scan pairs them, and what is
    asserted is what survived the journey: the match's stored budget, the
    seat's ladder, and — once both accept — a running clock with a deadline
    a worker could adjudicate.

    One test rather than four, deliberately. The steps are not independently
    reachable: there is no way to have a match without a scan, and asserting
    "the clock started" separately would mean pairing twice. What would be
    lost by splitting is the property that actually matters — that the
    *same* control chosen at the queue is the one the clock runs on.
    """

    async def test_the_chosen_control_reaches_the_match_its_clock_and_its_rating(
        self,
        client: AsyncClient,
        contract_session: AsyncSession,
        deadlines: RecordingClockDeadlines,
    ) -> None:
        alice = await register(client)
        bob = await register(client)
        for player in (alice, bob):
            joined = await client.post(
                QUEUE_URL,
                headers=player.auth,
                json={"queue_type": "ranked", "time_control_id": BLITZ.value},
            )
            assert joined.status_code == 201, joined.text

        outcome = await _scan(contract_session, BLITZ)
        assert outcome.match_id is not None, "the two tickets should have paired"

        # --- the durable snapshot (§13) -------------------------------
        record = await SqlAlchemyMatchRecordRepository(contract_session).by_id(outcome.match_id)
        assert record is not None
        assert record.time_control is not None
        assert record.time_control.initial_ms == 180_000
        assert record.time_control.increment_ms == 2_000

        # --- the rating key (§15) — blitz, not `DEFAULT_SPEED_CLASS` ---
        assert record.light.rating is not None
        assert record.light.rating.speed_class == SpeedClass.BLITZ.value

        # --- the acceptance dialog sees it (§10) ----------------------
        offered = await client.get(PENDING_URL, headers=alice.auth)
        assert offered.status_code == 200, offered.text
        assert offered.json()["data"]["base_time_ms"] == 180_000
        assert offered.json()["data"]["increment_ms"] == 2_000
        assert offered.json()["data"]["speed_class"] == "blitz"

        # --- activation starts the clock and schedules the flag (§14) --
        for player in (alice, bob):
            answered = await client.post(_accept_url(outcome.match_id), headers=player.auth)
            assert answered.status_code == 200, answered.text

        activated = await SqlAlchemyMatchRecordRepository(contract_session).by_id(outcome.match_id)
        assert activated is not None
        assert activated.clock is not None
        assert activated.clock.light_ms == 180_000
        assert activated.clock.dark_ms == 180_000
        # The deadline is the running side's budget from the instant both
        # players agreed — not from creation, or LIGHT would be charged for
        # however long DARK took to answer.
        assert activated.clock.turn_started_at == activated.settled_at

        scheduled = [entry for entry in deadlines.scheduled if entry[0] == outcome.match_id]
        assert len(scheduled) == 1
        assert scheduled[0][3] == activated.clock.deadline()


class TestTwoControlsAreNeverPaired:
    async def test_a_scan_of_one_pool_cannot_see_another_control_s_ticket(
        self, client: AsyncClient, contract_session: AsyncSession
    ) -> None:
        """§6 and §21, at the layer that enforces them.

        Two players in the same variant, mode and region — differing only in
        the clock they chose — are not each other's opponents. The rule is
        the pool predicate's rather than the engine's, so it is asserted by
        running a real scan against a real table and finding nothing.

        The failure this prevents is not theoretical: a pool key that had
        omitted the control, or an index left at its old five columns, would
        pair a bullet player into a classical game and neither would learn
        why until the clock started.
        """
        alice = await register(client)
        bob = await register(client)
        for player, control in ((alice, "bullet_1_0"), (bob, "classical_30_0")):
            joined = await client.post(
                QUEUE_URL,
                headers=player.auth,
                json={"queue_type": "ranked", "time_control_id": control},
            )
            assert joined.status_code == 201, joined.text

        outcome = await _scan(contract_session, TimeControlId.BULLET_1_0)

        assert outcome.match_id is None
        assert outcome.scanned == 1, "the bullet pool holds one ticket, not two"


async def _scan(session: AsyncSession, time_control_id: TimeControlId) -> PairingOutcome:
    """One real pairing pass over one pool.

    Built through `matchmaking`'s own factories rather than by hand, so what
    runs here is the graph `app_factory` wires — the same argument
    `build_contract_app` makes for the HTTP path.

    `NoEventPublisher`, because these tests assert on rows rather than on
    outbox entries and a publisher would put `PlayersPaired` in a
    transaction nothing drains. Everything that decides *who* is paired —
    the repository, the engine, the rating window, the block read, the
    rematch guard, `game`'s creation port — is real.
    """
    clock = SystemClock()
    service = build_pairing_service(
        session,
        exclusions=build_pairing_exclusions(session),
        opponents=build_recent_opponents(session),
        matches=build_match_creation(session, events=NoEventPublisher(), clock=clock),
        events=NoEventPublisher(),
        settings=get_settings().matchmaking,
        clock=clock,
        metrics=RecordingMetrics(),
    )
    outcome = await service.pair_once(
        pool=QueuePool(
            variant=ProductVariant.RUSSIAN_8X8,
            queue_type=QueueType.RANKED,
            time_control_id=time_control_id,
            region=Region.GLOBAL,
        )
    )
    await session.commit()
    return outcome
