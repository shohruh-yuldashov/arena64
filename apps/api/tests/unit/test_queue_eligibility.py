"""`QueueEligibilityPolicy` — may this player enter a pool at all.

A64-015.2 turns A64-015.1's inline presence check into a port, and what is
tested here is the port's *contract* rather than the one implementation
behind it: it passes silently or raises `QueueNotPermitted`, and the refusal
says nothing about why.

The asymmetry in `PresenceEligibilityPolicy` is the substance. Presence
collapses "expired", "never recorded" and "Redis unreachable" into `None`,
so only a record positively saying `online: false` refuses. Getting that
backwards would make a cache blip an outage of matchmaking.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.core.identifiers import generate_uuid7
from app.modules.game.public import ProductVariant
from app.modules.matchmaking.application.eligibility import (
    AlwaysEligible,
    PresenceEligibilityPolicy,
    QueueEligibilityPolicy,
)
from app.modules.matchmaking.domain.exceptions import QueueNotPermitted
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType, Region
from tests.fakes.queue_repository import StubPresence

NOW = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
PLAYER = generate_uuid7()

POOL = QueuePool(
    variant=ProductVariant.RUSSIAN_8X8, queue_type=QueueType.RANKED, region=Region.EUROPE
)


@pytest.fixture
def presence() -> StubPresence:
    return StubPresence()


@pytest.fixture
def policy(presence: StubPresence) -> PresenceEligibilityPolicy:
    return PresenceEligibilityPolicy(presence)


class TestPresenceEligibility:
    async def test_a_player_recorded_online_may_queue(
        self, policy: PresenceEligibilityPolicy, presence: StubPresence
    ) -> None:
        """Permission is silence — the port is a command rather than a
        predicate, so "does not raise" is the whole assertion."""
        presence.online(PLAYER, at=NOW)

        await policy.require_eligible(PLAYER, pool=POOL)

    async def test_a_player_recorded_offline_may_not(
        self, policy: PresenceEligibilityPolicy, presence: StubPresence
    ) -> None:
        presence.offline(PLAYER, at=NOW)

        with pytest.raises(QueueNotPermitted):
            await policy.require_eligible(PLAYER, pool=POOL)

    async def test_an_unobserved_player_may_queue(self, policy: PresenceEligibilityPolicy) -> None:
        """`None` is three situations at once and the provider's own
        docstring says a caller must not try to tell them apart. Permitting
        is the safe direction: the alternative is that an unreachable Redis
        stops everybody queueing."""
        await policy.require_eligible(PLAYER, pool=POOL)

    async def test_the_refusal_names_no_cause(
        self, policy: PresenceEligibilityPolicy, presence: StubPresence
    ) -> None:
        """§8 and BL-1: this port will grow checks about sanctions and
        block relationships, and a message that varied by cause would let a
        player probe them by queueing repeatedly. One message, from the
        first check onwards."""
        presence.offline(PLAYER, at=NOW)

        with pytest.raises(QueueNotPermitted) as refusal:
            await policy.require_eligible(PLAYER, pool=POOL)

        message = str(refusal.value).lower()
        assert "offline" not in message
        assert "presence" not in message
        assert "block" not in message

    async def test_the_pool_does_not_change_the_answer_today(
        self, policy: PresenceEligibilityPolicy, presence: StubPresence
    ) -> None:
        """The pool is a parameter because a future check is per-pool — a
        regional lockout, a variant withdrawn mid-season. Presence is not
        one, and this pins that the argument is not being read."""
        presence.offline(PLAYER, at=NOW)
        casual = QueuePool(variant=POOL.variant, queue_type=QueueType.CASUAL)

        with pytest.raises(QueueNotPermitted):
            await policy.require_eligible(PLAYER, pool=casual)

    async def test_one_player_s_record_does_not_refuse_another(
        self, policy: PresenceEligibilityPolicy, presence: StubPresence
    ) -> None:
        presence.offline(PLAYER, at=NOW)
        somebody_else = generate_uuid7()

        await policy.require_eligible(somebody_else, pool=POOL)

    async def test_it_cannot_write_presence(self, policy: PresenceEligibilityPolicy) -> None:
        """It holds a `PresenceProvider` and not a `PresenceRecorder`. The
        port it does not hold is what guarantees a read-only policy, rather
        than a convention somebody later forgets."""
        assert not hasattr(policy._presence, "record_online")


class TestAlwaysEligible:
    async def test_it_permits_everybody(self) -> None:
        """For a deployment with presence disabled, and for tests whose
        subject is something else. A real implementation rather than a
        mock, so the composition root has something to wire."""
        await AlwaysEligible().require_eligible(PLAYER, pool=POOL)

    async def test_it_needs_no_collaborator(self) -> None:
        assert isinstance(AlwaysEligible(), AlwaysEligible)


class TestThePortIsStructural:
    def test_both_implementations_satisfy_it(self, presence: StubPresence) -> None:
        """A `Protocol`, so a future `SanctionEligibilityPolicy` in `admin`
        needs no import from `matchmaking` to be wireable here."""
        policies: list[QueueEligibilityPolicy] = [
            PresenceEligibilityPolicy(presence),
            AlwaysEligible(),
        ]

        assert len(policies) == 2

    async def test_a_test_may_supply_its_own(self) -> None:
        """Which is the point of the seam — a queue test that is not about
        eligibility should not have to construct a presence store."""

        class Refusing:
            async def require_eligible(self, player_id: UUID, *, pool: QueuePool) -> None:
                raise QueueNotPermitted("You cannot join a queue right now.")

        policy: QueueEligibilityPolicy = Refusing()

        with pytest.raises(QueueNotPermitted):
            await policy.require_eligible(PLAYER, pool=POOL)
