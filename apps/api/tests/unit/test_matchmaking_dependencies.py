"""The matchmaking composition root — A64-015.2.

`presentation/dependencies/` is where every collaborator is chosen, and the
two things worth asserting about it are the two that are invisible from a
route: which adapter satisfies a port, and how many instances exist.

`build_*` and `get_*` are separate on purpose — the expiry task has no
request to hang `Depends` off, so it calls the builders directly through
`app_factory`. A test that only exercised the `Depends` half would leave the
worker's graph untested.
"""

from app.modules.game.public import GameEngineServices, engine_services
from app.modules.matchmaking.application.eligibility import PresenceEligibilityPolicy
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


class TestTheEligibilityPortIsSatisfiedByTheRoot:
    def test_the_presence_backed_policy_is_chosen(self) -> None:
        """The service depends on the port; the root decides the adapter.
        Which one it picked is otherwise only observable by making a player
        offline and watching a join fail."""
        policy = build_eligibility_policy(StubPresence())

        assert isinstance(policy, PresenceEligibilityPolicy)

    def test_the_policy_is_built_per_call_rather_than_shared(self) -> None:
        """It holds a request-scoped presence reader, so caching it would
        outlive the reader it wraps — the opposite of the engine bundle
        above, and the reason the two are wired differently."""
        presence = StubPresence()

        assert build_eligibility_policy(presence) is not build_eligibility_policy(presence)
