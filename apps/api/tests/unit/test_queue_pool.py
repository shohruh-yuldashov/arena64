"""`QueuePool` — the value that says which queue a ticket is waiting in.

Three enums and one guard, and the guard is the interesting part: a pool
names a `ProductVariant`, and the constructor re-asks `game.public` whether
that variant is still offered. A pool for a withdrawn variant must not
construct anywhere — including in a repository rehydrating a row written
before the withdrawal.
"""

from dataclasses import FrozenInstanceError

import pytest

from app.modules.game.public import ProductVariant, VariantNotOffered
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType, Region
from app.modules.reference.public import TimeControlId

RUSSIAN = ProductVariant.RUSSIAN_8X8
BLITZ = TimeControlId.BLITZ_3_2


def _pool(
    *,
    variant: ProductVariant = RUSSIAN,
    queue_type: QueueType = QueueType.RANKED,
    time_control_id: TimeControlId = BLITZ,
    region: Region = Region.GLOBAL,
) -> QueuePool:
    return QueuePool(
        variant=variant,
        queue_type=queue_type,
        time_control_id=time_control_id,
        region=region,
    )


class TestConstruction:
    def test_the_offered_variant_is_accepted(self) -> None:
        assert _pool().variant is RUSSIAN

    def test_the_region_defaults_to_global(self) -> None:
        """An unlocated player is pairable with everybody rather than with
        nobody — see the enum's docstring on why `GLOBAL` is not a place."""
        pool = QueuePool(variant=RUSSIAN, queue_type=QueueType.CASUAL, time_control_id=BLITZ)

        assert pool.region is Region.GLOBAL

    def test_the_time_control_has_no_default(self) -> None:
        """A64-020.5A-pre §16 forbids one, and the reason is that no value
        is a safe guess: every control is a different game, so a caller who
        omitted it would enter a pool they did not pick.

        Asserted rather than trusted, because a default is one keyword away
        and nothing else on this type would notice it appearing."""
        with pytest.raises(TypeError):
            QueuePool(variant=RUSSIAN, queue_type=QueueType.RANKED)  # type: ignore[call-arg]

    def test_a_variant_the_platform_does_not_offer_is_refused(self) -> None:
        """The engine plays English 8x8 and no player may queue for it
        (`specs/game-engine/audit.md` §9). `ProductVariant` cannot express
        it, so this reaches the guard the way a rehydrated row would — by
        constructing with a value that is not in the catalogue."""
        with pytest.raises(VariantNotOffered):
            QueuePool(
                variant="english_8x8",  # type: ignore[arg-type]
                queue_type=QueueType.RANKED,
                time_control_id=BLITZ,
            )

    def test_the_primitive_form_is_normalised_to_the_enum(self) -> None:
        """The guard checks *and* converts, so a pool is never a
        `ProductVariant` by annotation and a `str` in fact — which would
        survive equality (a `StrEnum` compares equal to its value) and fail
        later wherever the enum's own members are used."""
        pool = QueuePool(
            variant="russian_8x8",  # type: ignore[arg-type]
            queue_type=QueueType.RANKED,
            time_control_id=BLITZ,
        )

        assert pool.variant is RUSSIAN

    def test_the_refusal_happens_at_construction_and_not_at_use(self) -> None:
        """A pool that validated lazily would be carried through a service
        call, a repository write and an index before failing — at which
        point the failure names the query rather than the value."""
        with pytest.raises(VariantNotOffered):
            QueuePool(
                variant="draughts_64",  # type: ignore[arg-type]
                queue_type=QueueType.CASUAL,
                time_control_id=BLITZ,
            )


class TestIdentity:
    def test_two_pools_built_from_the_same_choices_are_equal(self) -> None:
        """A pairing scan groups tickets by pool and a metric is labelled
        by one. Both break quietly under identity equality."""
        assert _pool(region=Region.ASIA) == _pool(region=Region.ASIA)

    def test_a_different_region_is_a_different_pool(self) -> None:
        assert _pool(region=Region.ASIA) != _pool(region=Region.EUROPE)

    def test_a_different_mode_is_a_different_pool(self) -> None:
        assert _pool(queue_type=QueueType.RANKED) != _pool(queue_type=QueueType.CASUAL)

    def test_a_different_time_control_is_a_different_pool(self) -> None:
        """A64-020.5A-pre §6, and the reason the key is the control rather
        than its speed class: 3+2 and 1+0 are both fast, and two players who
        chose them are not each other's opponents."""
        assert _pool(time_control_id=TimeControlId.BLITZ_3_2) != _pool(
            time_control_id=TimeControlId.BULLET_1_0
        )

    def test_a_pool_is_usable_as_a_dictionary_key(self) -> None:
        """Which is how a scan groups, and it only works because the record
        is frozen."""
        counts = {_pool(): 1}
        counts[_pool()] += 1

        assert counts == {_pool(): 2}

    def test_a_pool_cannot_be_reassigned(self) -> None:
        pool = _pool()

        with pytest.raises(FrozenInstanceError):
            pool.region = Region.AFRICA  # type: ignore[misc]


class TestTheIdentifier:
    def test_it_reads_widest_to_narrowest(self) -> None:
        pool = _pool(queue_type=QueueType.CASUAL, region=Region.EUROPE)

        assert pool.identifier() == "russian_8x8:casual:blitz_3_2:europe"

    def test_it_is_what_the_pool_prints_as(self) -> None:
        """So a log line or an f-string carries the pool rather than a
        dataclass repr."""
        pool = _pool()

        assert f"{pool}" == pool.identifier()

    def test_distinct_pools_have_distinct_identifiers(self) -> None:
        """It is a key, not a label. Two pools colliding would merge two
        scans into one."""
        every = [
            QueuePool(
                variant=variant,
                queue_type=queue_type,
                time_control_id=time_control_id,
                region=region,
            )
            for variant in ProductVariant
            for queue_type in QueueType
            for time_control_id in TimeControlId
            for region in Region
        ]

        assert len({pool.identifier() for pool in every}) == len(every)

    def test_it_carries_no_player_information(self) -> None:
        """It reaches logs and metric labels, where a player identifier
        would be personal data on a high-cardinality dimension."""
        assert _pool().identifier().count(":") == 3
