"""`PairingTask` and the pool's wire format — A64-015.3 §13.

A pool crosses the task boundary as one primitive string, so the identifier
stopped being a label and became a wire format. What is asserted here is the
round trip and its refusals: a payload that cannot be parsed must fail
loudly rather than scan some default queue.

The handler itself is four lines, and one test that it reaches the service
with the pool the payload named is the whole of what there is to check —
`PairingService` is tested in `test_pairing_service.py`.
"""

from collections.abc import Mapping
from typing import Any

import pytest

from app.modules.game.public import ProductVariant, VariantNotOffered
from app.modules.matchmaking.domain.queue_pool import (
    QueuePool,
    QueueType,
    Region,
    every_pool,
)
from app.modules.matchmaking.infrastructure import (
    MATCHMAKING_QUEUE,
    PAIRING_POOL_KEY,
    PAIRING_TASK,
    PairingTask,
    pairing_request,
)

POOL = QueuePool(
    variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.CASUAL, region=Region.ASIA
)


class TestThePoolWireFormat:
    def test_an_identifier_round_trips(self) -> None:
        """The property the task payload depends on. Without it a scheduled
        scan would read a different pool from the one it was scheduled
        for."""
        assert QueuePool.from_identifier(POOL.identifier()) == POOL

    def test_every_pool_round_trips(self) -> None:
        for pool in every_pool():
            assert QueuePool.from_identifier(pool.identifier()) == pool

    def test_a_malformed_identifier_is_refused(self) -> None:
        """A payload only this repository's own scheduler produces, so a
        bad one is a bug in the dispatcher. Failing loudly beats scanning
        some default pool quietly."""
        with pytest.raises(ValueError):
            QueuePool.from_identifier("russian_8x8:ranked")

    def test_an_unknown_region_is_refused(self) -> None:
        with pytest.raises(ValueError):
            QueuePool.from_identifier("russian_8x8:ranked:mars")

    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValueError):
            QueuePool.from_identifier("russian_8x8:tournament:global")

    def test_a_variant_that_is_not_offered_is_refused(self) -> None:
        """The engine plays English 8x8; no player may queue for it, and no
        scheduler may scan for it either."""
        with pytest.raises(VariantNotOffered):
            QueuePool.from_identifier("english_8x8:ranked:global")


class TestTheRequest:
    def test_it_names_the_pairing_task(self) -> None:
        assert pairing_request(POOL).name == PAIRING_TASK

    def test_it_routes_to_the_matchmaking_queue(self) -> None:
        """Its own SLO class (AD-20): a slow retention prune must not be
        able to delay a pairing scan."""
        assert pairing_request(POOL).queue == MATCHMAKING_QUEUE

    def test_the_payload_carries_one_primitive(self) -> None:
        """§13 forbids serialising a repository or a framework object. A
        pool identifier is a string, and that is the whole payload."""
        payload = pairing_request(POOL).payload

        assert payload == {PAIRING_POOL_KEY: "russian_8x8:casual:asia"}
        assert all(isinstance(value, str) for value in payload.values())

    def test_two_pools_produce_two_different_requests(self) -> None:
        """One request per pool is what makes a scan pool-scoped at the
        scheduling layer rather than only inside the service."""
        other = QueuePool(variant=POOL.variant, queue_type=QueueType.RANKED)

        assert pairing_request(POOL).payload != pairing_request(other).payload


class _RecordingPairingService:
    """Records the pool it was asked to scan."""

    def __init__(self) -> None:
        self.pools: list[QueuePool] = []

    async def pair_once(self, *, pool: QueuePool) -> None:
        self.pools.append(pool)


class _NullSessionFactory:
    """A session factory whose session does nothing.

    The handler's only job besides parsing is opening one session per run;
    what it opens is the composition root's business, and a real one would
    make this a database test for no gain.
    """

    def __call__(self) -> "_NullSessionFactory":
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class TestTheHandler:
    @pytest.fixture
    def service(self) -> _RecordingPairingService:
        return _RecordingPairingService()

    @pytest.fixture
    def task(self, service: _RecordingPairingService) -> PairingTask:
        return PairingTask(
            session_factory=_NullSessionFactory(),  # type: ignore[arg-type]
            service_factory=lambda _session: service,  # type: ignore[arg-type,return-value]
        )

    def test_it_answers_to_the_pairing_task_name(self, task: PairingTask) -> None:
        """The dispatcher routes by this string; a mismatch means the
        scheduler fires into nothing."""
        assert task.name == PAIRING_TASK

    async def test_it_scans_the_pool_the_payload_names(
        self, task: PairingTask, service: _RecordingPairingService
    ) -> None:
        payload: Mapping[str, Any] = {PAIRING_POOL_KEY: POOL.identifier()}

        await task.run(payload)

        assert service.pools == [POOL]

    async def test_a_malformed_payload_raises(
        self, task: PairingTask, service: _RecordingPairingService
    ) -> None:
        """Recorded by `InlineTaskDispatcher.dispatch`, which is where a
        task's failure is logged. Defaulting to a pool would scan the wrong
        queue silently."""
        with pytest.raises(ValueError):
            await task.run({PAIRING_POOL_KEY: "nonsense"})

        assert service.pools == []

    async def test_a_missing_pool_raises(self, task: PairingTask) -> None:
        with pytest.raises(KeyError):
            await task.run({})


class TestThePoolCatalogue:
    def test_it_covers_every_combination(self) -> None:
        """One scheduler per pool, so a pool missing from here is a queue
        nothing ever scans."""
        assert len(every_pool()) == len(ProductVariant) * len(QueueType) * len(Region)

    def test_every_pool_is_distinct(self) -> None:
        assert len({pool.identifier() for pool in every_pool()}) == len(every_pool())

    def test_the_order_is_stable(self) -> None:
        assert every_pool() == every_pool()

    def test_it_offers_no_variant_a_player_cannot_choose(self) -> None:
        assert {pool.variant for pool in every_pool()} == set(ProductVariant)
