"""`PresenceAudienceService` — the fan-out block filter, A64-013.6.

The brief's hardest constraint is here: "blocked pairs must never receive
presence updates about each other. Filtering belongs at the source of
fan-out. Do not rely on clients filtering events."

Nothing fans out yet — WebSockets are excluded — so what is asserted is the
*source*: the set a future gateway will send to. A test that waited for the
transport would leave the one rule the brief states in absolute terms
unenforced until the task that is not allowed to add it.

Two properties, and both are named in the brief:

    blocking      no blocked player is ever in the audience, whichever
                  direction the block was placed in
    performance   the set costs two reads regardless of its size — no
                  per-friend "is this one blocked" query

The repositories are counting fakes rather than the real ones: what is under
test is the *subtraction*, and `blocked_ids_for`'s symmetry already has
contract coverage against PostgreSQL in `test_blocking_api.py`.
"""

from uuid import UUID

import pytest

from app.modules.friends.application.services import PresenceAudienceService

PLAYER = UUID("019fbb3a-1c25-7e46-8a17-9b2c4d5e6f70")
FRIEND = UUID("019fbb3a-2d36-7f57-9b28-0c3d5e6f7a81")
ANOTHER_FRIEND = UUID("019fbb3a-3e47-7a68-8c39-1d4e6f7a8b92")
BLOCKED_FRIEND = UUID("019fbb3a-4f58-7b79-9d4a-2e5f7a8b9c03")
STRANGER = UUID("019fbb3a-5a69-7c8a-8e5b-3f6a8b9c0d14")


class _StubFriendships:
    def __init__(self, friends: set[UUID]) -> None:
        self._friends = friends
        self.calls = 0

    async def friend_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        self.calls += 1
        return frozenset(self._friends)


class _StubBlocks:
    def __init__(self, blocked: set[UUID]) -> None:
        self._blocked = blocked
        self.calls = 0

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        self.calls += 1
        return frozenset(self._blocked)


def _service(*, friends: set[UUID], blocked: set[UUID]) -> PresenceAudienceService:
    return PresenceAudienceService(
        friendships=_StubFriendships(friends),  # type: ignore[arg-type]
        blocks=_StubBlocks(blocked),  # type: ignore[arg-type]
    )


class TestWhoReceivesPresence:
    async def test_friends_are_the_audience(self) -> None:
        """Presence is pushed to people with a reason to receive it.

        A stranger reads presence through the composer when they open a
        profile; they do not subscribe to it.
        """
        service = _service(friends={FRIEND, ANOTHER_FRIEND}, blocked=set())

        assert await service.observers_of(PLAYER) == frozenset({FRIEND, ANOTHER_FRIEND})

    async def test_a_player_with_no_friends_has_no_audience(self) -> None:
        """The common case on a young platform, and a gateway must read it
        as "send nothing" rather than "send to everybody" — which is why
        this returns a set and not a predicate."""
        service = _service(friends=set(), blocked=set())

        assert await service.observers_of(PLAYER) == frozenset()

    async def test_strangers_are_never_in_the_audience(self) -> None:
        service = _service(friends={FRIEND}, blocked=set())

        assert STRANGER not in await service.observers_of(PLAYER)


class TestBlockedPlayersNeverReceivePresence:
    async def test_a_blocked_player_is_removed_from_the_audience(self) -> None:
        """The brief's absolute rule.

        Blocking already ends friendships (FS-3), so this case should not
        arise from the database — and the subtraction runs anyway, because
        "should not arise" is a claim about another transaction and a
        fan-out filter is the wrong place to assume one held.
        """
        service = _service(friends={FRIEND, BLOCKED_FRIEND}, blocked={BLOCKED_FRIEND})

        audience = await service.observers_of(PLAYER)

        assert BLOCKED_FRIEND not in audience
        assert audience == frozenset({FRIEND})

    async def test_the_direction_of_the_block_does_not_matter(self) -> None:
        """`blocked_ids_for` is symmetric — it returns players this one
        blocked *and* players who blocked this one — so a blocked pair is
        excluded whichever way round it was placed. A blocked player who
        kept receiving presence would learn nothing from a frame; a
        *blocker* who kept receiving one would have gained nothing from
        blocking."""
        service = _service(friends={BLOCKED_FRIEND}, blocked={BLOCKED_FRIEND})

        assert await service.observers_of(PLAYER) == frozenset()

    async def test_a_block_against_somebody_who_is_not_a_friend_changes_nothing(self) -> None:
        """Subtracting a set that does not intersect is a no-op, and must
        not remove anybody else."""
        service = _service(friends={FRIEND}, blocked={STRANGER})

        assert await service.observers_of(PLAYER) == frozenset({FRIEND})

    async def test_the_audience_cannot_be_added_to_by_a_caller(self) -> None:
        """A `frozenset`, so a gateway cannot append a recipient the filter
        excluded — the one mistake that would defeat filtering at the
        source."""
        service = _service(friends={FRIEND}, blocked=set())

        assert isinstance(await service.observers_of(PLAYER), frozenset)


class TestQueryCount:
    async def test_the_audience_costs_two_reads_whatever_its_size(self) -> None:
        """No N+1. A version that asked "is this recipient blocked" per
        friend would issue a query per recipient on a path that runs on
        every presence transition — the pattern A64-013.6 names outright."""
        friendships = _StubFriendships({FRIEND, ANOTHER_FRIEND, BLOCKED_FRIEND})
        blocks = _StubBlocks({BLOCKED_FRIEND})
        service = PresenceAudienceService(
            friendships=friendships,  # type: ignore[arg-type]
            blocks=blocks,  # type: ignore[arg-type]
        )

        await service.observers_of(PLAYER)

        assert friendships.calls == 1
        assert blocks.calls == 1

    async def test_a_player_with_no_friends_never_reads_the_block_set(self) -> None:
        """Nothing to filter, so nothing to fetch — the guard that keeps the
        most common transition on the platform down to a single read."""
        friendships = _StubFriendships(set())
        blocks = _StubBlocks({BLOCKED_FRIEND})
        service = PresenceAudienceService(
            friendships=friendships,  # type: ignore[arg-type]
            blocks=blocks,  # type: ignore[arg-type]
        )

        await service.observers_of(PLAYER)

        assert blocks.calls == 0


class TestWhatThisServiceDeliberatelyDoesNot:
    async def test_it_applies_no_privacy_setting(self) -> None:
        """A64-013.6: "no permission logic outside the composer."

        The audience is *social* — friends minus blocked — and whether a
        given recipient may see a presence field is `VisibilityLevel`
        applied by `PublicProfileComposer`. This asserts the absence: a
        friend is in the audience regardless of any setting, because this
        service never reads one. A future gateway renders each recipient's
        view through the composer; membership here is not permission.
        """
        service = _service(friends={FRIEND}, blocked=set())

        assert FRIEND in await service.observers_of(PLAYER)

    async def test_the_player_is_not_their_own_observer(self) -> None:
        """Nobody is their own friend, so the audience never contains the
        subject — a gateway echoing a frame back to its origin would be a
        loop, not a feature."""
        service = _service(friends={FRIEND}, blocked=set())

        assert PLAYER not in await service.observers_of(PLAYER)


@pytest.mark.parametrize(
    ("friends", "blocked", "expected"),
    [
        (set(), set(), frozenset()),
        ({FRIEND}, set(), frozenset({FRIEND})),
        ({FRIEND}, {FRIEND}, frozenset()),
        ({FRIEND, ANOTHER_FRIEND}, {ANOTHER_FRIEND}, frozenset({FRIEND})),
    ],
    ids=["nobody", "one-friend", "friend-blocked", "one-of-two-blocked"],
)
async def test_the_audience_is_friends_minus_blocked(
    friends: set[UUID], blocked: set[UUID], expected: frozenset[UUID]
) -> None:
    """The whole rule, as a table — `observers_of(p) = friends(p) - blocked(p)`."""
    service = _service(friends=friends, blocked=blocked)

    assert await service.observers_of(PLAYER) == expected
