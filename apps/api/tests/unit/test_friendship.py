"""The `Friendship` aggregate and the relationship providers — the two
pieces of A64-013.3 that are pure logic.

Everything about *storing* friendships needs PostgreSQL and lives in
`tests/contract/test_friends_api.py`. What is here is the canonical-pair
invariant, the participation rules, and the provider that turns a social
graph into a `ViewerRelationship` — all of which are correctness controls,
and none of which needs a database to assert.

A64-013.3 asks for essential tests only. Canonical ordering and duplicate
prevention are named there; the rest is the small set of properties that
would be *silently* wrong rather than loudly broken — a mirrored row that
looks fine, or a fallback that widens visibility instead of narrowing it.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.modules.friends.domain.exceptions import (
    FriendshipAlreadyEnded,
    NotFriendshipParticipant,
    SelfFriendship,
)
from app.modules.friends.domain.friendship import (
    Friendship,
    FriendshipEndReason,
    canonical_pair,
)
from app.modules.profiles.infrastructure import (
    FriendshipRelationshipProvider,
    NoRelationshipsProvider,
)
from app.modules.users.domain.visibility import ViewerRelationship

FORMED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
ENDED_AT = datetime(2026, 8, 2, 9, 30, tzinfo=UTC)

# Deliberately out of numeric order, so every test below that passes them
# as `(HIGH, LOW)` is exercising the sort rather than agreeing with it.
LOW = UUID("019fb9ea-0a0c-7cec-9c5f-402727c31a96")
HIGH = UUID("019fb9ea-1b1d-7ded-8b60-513838d42b07")
STRANGER = UUID("019fb9ea-2c2e-7eee-9c71-624949e53c18")


class TestCanonicalPair:
    """DB-12: symmetric relationships are stored once, in canonical
    identifier order."""

    def test_the_pair_is_sorted_whichever_way_it_arrives(self) -> None:
        assert canonical_pair(LOW, HIGH) == (LOW, HIGH)
        assert canonical_pair(HIGH, LOW) == (LOW, HIGH)

    def test_a_friendship_stores_the_pair_in_order(self) -> None:
        """The caller passes requester and addressee in whatever order the
        request happened to have; `between` sorts, so no caller has to know
        about `low` and `high` and none can get them wrong."""
        friendship = Friendship.between(HIGH, LOW, created_at=FORMED_AT)

        assert friendship.player_low_id == LOW
        assert friendship.player_high_id == HIGH

    def test_both_orderings_produce_the_same_row_shape(self) -> None:
        """The property that makes a mirrored row impossible: the two
        directions are not two facts, they are one."""
        forward = Friendship.between(LOW, HIGH, created_at=FORMED_AT)
        reverse = Friendship.between(HIGH, LOW, created_at=FORMED_AT)

        assert (forward.player_low_id, forward.player_high_id) == (
            reverse.player_low_id,
            reverse.player_high_id,
        )

    def test_a_mis_ordered_row_is_refused_on_rehydration(self) -> None:
        """The repository constructs instances directly when reading rows,
        so `__post_init__` is what stops a hand-written `INSERT` in a repair
        script from reaching a response. `ck_friendship__canonical_order` is
        the authoritative copy (BE-06)."""
        with pytest.raises(ValueError, match="canonical order"):
            Friendship(player_low_id=HIGH, player_high_id=LOW)

    def test_a_player_cannot_befriend_themselves(self) -> None:
        with pytest.raises(SelfFriendship):
            Friendship.between(LOW, LOW, created_at=FORMED_AT)


class TestLifecycle:
    def test_a_new_friendship_is_live(self) -> None:
        friendship = Friendship.between(LOW, HIGH, created_at=FORMED_AT)

        assert friendship.is_live is True
        assert friendship.created_at == FORMED_AT
        assert friendship.ended_at is None
        assert friendship.ended_reason is None

    def test_either_party_may_end_it(self) -> None:
        """FS-2: removal is unilateral. "Requiring mutual agreement to stop
        being friends is not a feature anyone wants"."""
        for actor in (LOW, HIGH):
            friendship = Friendship.between(LOW, HIGH, created_at=FORMED_AT)

            friendship.end(by=actor, at=ENDED_AT, reason=FriendshipEndReason.REMOVED)

            assert friendship.is_live is False
            assert friendship.ended_at == ENDED_AT
            assert friendship.ended_reason is FriendshipEndReason.REMOVED

    def test_a_stranger_cannot_end_it(self) -> None:
        friendship = Friendship.between(LOW, HIGH, created_at=FORMED_AT)

        with pytest.raises(NotFriendshipParticipant):
            friendship.end(by=STRANGER, at=ENDED_AT, reason=FriendshipEndReason.REMOVED)

        assert friendship.is_live is True

    def test_the_rejection_names_neither_participant(self) -> None:
        """A rejection that named one would turn a guessed identifier into a
        way to learn who is friends with whom — the thing a friends-only
        visibility setting exists to control."""
        friendship = Friendship.between(LOW, HIGH, created_at=FORMED_AT)

        with pytest.raises(NotFriendshipParticipant) as refused:
            friendship.end(by=STRANGER, at=ENDED_AT, reason=FriendshipEndReason.REMOVED)

        assert str(LOW) not in refused.value.message
        assert str(HIGH) not in refused.value.message

    def test_it_cannot_be_ended_twice(self) -> None:
        """Exactly one transition out of the live state — the same invariant
        `FriendRequest._resolve` holds, and reachable the same way, by two
        devices removing at once."""
        friendship = Friendship.between(LOW, HIGH, created_at=FORMED_AT)
        friendship.end(by=LOW, at=ENDED_AT, reason=FriendshipEndReason.REMOVED)

        with pytest.raises(FriendshipAlreadyEnded):
            friendship.end(by=HIGH, at=ENDED_AT, reason=FriendshipEndReason.REMOVED)

    def test_participation_is_checked_before_the_ended_state(self) -> None:
        """A stranger probing an ended friendship learns that they are not
        part of it, not what state it is in."""
        friendship = Friendship.between(LOW, HIGH, created_at=FORMED_AT)
        friendship.end(by=LOW, at=ENDED_AT, reason=FriendshipEndReason.REMOVED)

        with pytest.raises(NotFriendshipParticipant):
            friendship.end(by=STRANGER, at=ENDED_AT, reason=FriendshipEndReason.REMOVED)

    def test_blocking_is_a_prepared_end_reason(self) -> None:
        """A64-013.5 writes it. Declared now so the PostgreSQL enum contains
        it before anything needs to — `ALTER TYPE ... ADD VALUE` on a type
        used by a live table is a migration nobody should have to schedule
        to ship a feature."""
        assert FriendshipEndReason.BLOCKED in set(FriendshipEndReason)


class TestOtherThan:
    def test_it_returns_the_participant_who_is_not_the_viewer(self) -> None:
        """What a friend list renders: the viewer knows who they are and
        wants the other person."""
        friendship = Friendship.between(LOW, HIGH, created_at=FORMED_AT)

        assert friendship.other_than(LOW) == HIGH
        assert friendship.other_than(HIGH) == LOW

    def test_it_refuses_a_player_who_is_not_in_the_pair(self) -> None:
        """Rather than returning an arbitrary side. A silent wrong answer
        here would render a stranger as a friend."""
        friendship = Friendship.between(LOW, HIGH, created_at=FORMED_AT)

        with pytest.raises(NotFriendshipParticipant):
            friendship.other_than(STRANGER)


class _StubFriendshipReader:
    """The published port, answering from a fixed set.

    A stub rather than the real repository because what is under test here
    is the *mapping* from a set of ids to `ViewerRelationship` — the query
    itself is PostgreSQL's and is covered in
    `tests/contract/test_friends_api.py`.
    """

    def __init__(self, friends: set[UUID]) -> None:
        self._friends = friends
        self.calls = 0

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        self.calls += 1
        return {other for other in others if other in self._friends}


class TestRelationshipProviders:
    async def test_a_friend_resolves_to_friend_and_everybody_else_to_stranger(self) -> None:
        provider = FriendshipRelationshipProvider(_StubFriendshipReader({HIGH}))

        resolved = await provider.relationships_for(LOW, [HIGH, STRANGER])

        assert resolved[HIGH] is ViewerRelationship.FRIEND
        assert resolved[STRANGER] is ViewerRelationship.STRANGER

    async def test_the_mapping_is_complete(self) -> None:
        """Every id asked for has an entry, so a caller indexes rather than
        writing a fallback at each site — the line somebody eventually
        writes as `.get(id)` alone and then treats `None` as truthy on a
        privacy path."""
        provider = FriendshipRelationshipProvider(_StubFriendshipReader(set()))

        resolved = await provider.relationships_for(LOW, [HIGH, STRANGER])

        assert set(resolved) == {HIGH, STRANGER}

    async def test_a_page_costs_one_query(self) -> None:
        """The batch read behind every profile render. A per-player form
        would multiply the composition path by the page size — CLAUDE.md
        §10.4's N+1, on the hottest read on the platform."""
        reader = _StubFriendshipReader({HIGH})
        provider = FriendshipRelationshipProvider(reader)

        await provider.relationships_for(LOW, [HIGH, STRANGER, LOW])

        assert reader.calls == 1

    async def test_an_empty_page_costs_no_query(self) -> None:
        reader = _StubFriendshipReader({HIGH})
        provider = FriendshipRelationshipProvider(reader)

        assert await provider.relationships_for(LOW, []) == {}
        assert reader.calls == 0

    async def test_the_fallback_narrows_rather_than_widens(self) -> None:
        """The property that makes the kill switch safe to reach for during
        an incident: with the graph off, a friends-only field is hidden from
        everyone including actual friends. A visible loss of functionality,
        never a disclosure."""
        resolved = await NoRelationshipsProvider().relationships_for(LOW, [HIGH, STRANGER])

        assert set(resolved.values()) == {ViewerRelationship.STRANGER}
