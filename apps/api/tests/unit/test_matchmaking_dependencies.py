"""The matchmaking composition root — A64-015.2.

`presentation/dependencies/` is where every collaborator is chosen, and the
two things worth asserting about it are the two that are invisible from a
route: which adapter satisfies a port, and how many instances exist.

`build_*` and `get_*` are separate on purpose — the expiry task has no
request to hang `Depends` off, so it calls the builders directly through
`app_factory`. A test that only exercised the `Depends` half would leave the
worker's graph untested.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import SystemClock
from app.modules.game.public import GameEngineServices, engine_services
from app.modules.matchmaking.application.eligibility import (
    AllEligibilityChecks,
    CooldownEligibilityPolicy,
    PresenceEligibilityPolicy,
)
from app.modules.matchmaking.presentation.dependencies import (
    build_eligibility_policy,
    get_engine_services,
)
from tests.fakes.queue_repository import StubPresence


class TestEngineServicesAreWiredOnce:
    def test_the_dependency_hands_out_the_shared_instance(self) -> None:
        """Not a graph per request. Every engine collaborator is stateless
        (`specs/game-engine/audit.md` §14), so building one per call would
        be allocation with no behaviour attached."""
        assert get_engine_services() is engine_services()

    def test_two_requests_receive_the_same_object(self) -> None:
        assert get_engine_services() is get_engine_services()

    def test_it_is_the_published_bundle_and_not_a_local_one(self) -> None:
        """`matchmaking` does not construct engine collaborators — it asks
        `game.public` for them, which is what keeps the engine's version
        stamping and variant catalogue one authority (R-1, R-2)."""
        assert isinstance(get_engine_services(), GameEngineServices)


def _session() -> AsyncSession:
    """A session object the composition root can hold but nothing touches.

    Every factory under test constructs repositories over a session and
    none of these tests issues a query, so an unbound `AsyncSession` is
    exactly enough — and it keeps the assertion about *wiring* rather than
    about a database being reachable.
    """
    return AsyncSession()


class TestTheEligibilityPortIsSatisfiedByTheRoot:
    def test_both_checks_are_composed(self) -> None:
        """The service depends on one port; the root decides how many
        adapters satisfy it. A64-015.5 added the second — a decline
        cooldown — and `QueueService` did not change, which is what
        A64-015.2 predicted the port would buy."""
        policy = build_eligibility_policy(_session(), StubPresence(), clock=SystemClock())

        assert isinstance(policy, AllEligibilityChecks)

    def test_presence_is_asked_before_the_cooldown(self) -> None:
        """Order is significant: a player who fails both is refused by the
        check that says less. See `AllEligibilityChecks`."""
        policy = build_eligibility_policy(_session(), StubPresence(), clock=SystemClock())

        assert [type(check) for check in policy._checks] == [
            PresenceEligibilityPolicy,
            CooldownEligibilityPolicy,
        ]

    def test_the_policy_is_built_per_call_rather_than_shared(self) -> None:
        """It holds a request-scoped presence reader and a session-scoped
        cooldown store, so caching it would outlive both — the opposite of
        the engine bundle above, and the reason the two are wired
        differently."""
        presence = StubPresence()
        session = _session()

        first = build_eligibility_policy(session, presence, clock=SystemClock())
        second = build_eligibility_policy(session, presence, clock=SystemClock())

        assert first is not second
