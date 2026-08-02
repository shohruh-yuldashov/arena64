"""The gateway's two keyspaces against real Redis — A64-016.2 §1.

A64-016.1 shipped both with unit tests over fakes and recorded the gap in its
own known-gaps list: "`GETDEL`'s single-use guarantee and the registry's
atomic count are asserted against fakes that model them. A model that agrees
with itself proves nothing about Redis." This file closes it, and §1 names
the same thing — "the current fake tests are not sufficient to prove Redis
behavior".

Four tests, each of a property that is **unfalsifiable without a real
server**:

    GETDEL          one command, so exactly one of two callers gets the value
    MULTI/EXEC      the count comes from the same operation as the write
    ZADD / ZREM     a repeated register does not double-count and a repeated
                    unregister does not underflow
    the member      the node survives a round trip through a sorted set

A64-016.3 adds `gwroom:v1:`, which A64-016.2's known-gaps list named as the
next thing to cover:

    the room set    join, duplicate join, removal and the empty-room TTL,
                    with the member set returned from inside the write
    monotonic CAS   a concurrent update cannot silently overwrite a newer
                    sequence

Four tests for eight properties, because §12 caps this task at eight tests
in total and the four room behaviours take the other half. Each test
therefore carries the assertions that belong to one *mechanism* rather than
one line of code — which is also why the register/unregister test asserts
the repeat cases inline instead of in a fifth.

What is deliberately *not* here is anything the unit suite already covers —
the lifecycle, the membership rule, the presence transitions. §1 says not to
duplicate the fake-based tests and this does not: every assertion below would
pass trivially against a dictionary, which is precisely why it has to run
against Redis.

Skipped, not failed, when Redis is unreachable — the same contract every
other suite in this directory keeps.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest_asyncio
from redis.asyncio import Redis

from app.core.identifiers import generate_uuid7
from app.gateway.registry import RedisConnectionRegistry
from app.gateway.room_store import RedisRoomMemberStore
from app.gateway.rooms import RoomMember
from app.modules.auth.application.services.opaque_tokens import OpaqueTokenService
from app.modules.auth.infrastructure import RedisWebSocketTicketStore
from tests.fakes.presence_redis import MovableClock

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
TTL_SECONDS = 90

NODE_A = "gateway-a"
NODE_B = "gateway-b"

ROOM_TTL_SECONDS = 3600


@pytest_asyncio.fixture
async def tickets(contract_redis: Redis) -> RedisWebSocketTicketStore:
    return RedisWebSocketTicketStore(contract_redis)


@pytest_asyncio.fixture
async def registry(contract_redis: Redis) -> RedisConnectionRegistry:
    return RedisConnectionRegistry(contract_redis, clock=MovableClock(NOW))


def _digest(value: str) -> bytes:
    """The stored key for a ticket, through the platform's one hasher.

    `OpaqueTokenService` rather than a local `sha256`, because DB-24's whole
    argument is that there is exactly one implementation of "draw, hash,
    compare" — and a test that hashed independently could pass while the
    two disagreed.
    """
    return OpaqueTokenService().hash(value)


class TestTicketRedemptionIsSingleUse:
    """§1: "WebSocket ticket redemption uses atomic GETDEL semantics"."""

    async def test_a_ticket_redeems_once_and_is_then_gone(
        self, tickets: RedisWebSocketTicketStore
    ) -> None:
        """§12.1. The property AD-09 rests on, against the command that
        provides it.

        A fake models this by popping from a dictionary and proves nothing:
        what is under test is that `GETDEL` **deletes as part of the read**,
        so there is no window between the two in which a second caller sees
        a value that is about to be spent.
        """
        player_id, session_id = generate_uuid7(), generate_uuid7()
        await tickets.issue(
            _digest("one-shot"), player_id=player_id, session_id=session_id, ttl_seconds=30
        )

        first = await tickets.redeem(_digest("one-shot"))
        second = await tickets.redeem(_digest("one-shot"))

        assert first is not None
        assert first.player_id == player_id
        assert first.session_id == session_id
        assert second is None

    async def test_concurrent_redemption_has_exactly_one_winner(
        self, tickets: RedisWebSocketTicketStore
    ) -> None:
        """§12.2, and the reason the ticket is not a JWT.

        Ten redemptions of one ticket, launched together. A signed token
        would verify ten times; a check-then-delete would let several read
        before any deleted. `GETDEL` is one command, so Redis serialises
        them and exactly one receives the identity.

        This is not a contrived race — it is what a client opening a second
        tab while the first is still connecting produces, and it is the
        failure that would silently admit two sockets on one credential.
        """
        player_id = generate_uuid7()
        await tickets.issue(
            _digest("contended"), player_id=player_id, session_id=None, ttl_seconds=30
        )

        results = await asyncio.gather(*(tickets.redeem(_digest("contended")) for _ in range(10)))

        winners = [redeemed for redeemed in results if redeemed is not None]
        assert len(winners) == 1
        assert winners[0].player_id == player_id


class TestTheConnectionRegistryCountsAtomically:
    """§1: "connection registration updates the active count atomically" and
    "connection removal decrements the count atomically"."""

    async def test_the_count_tracks_register_and_unregister_exactly(
        self, registry: RedisConnectionRegistry
    ) -> None:
        """§12.3. The count comes back **from the write**, which is what
        makes the presence transition correct across a fleet.

        Three connections up and three down, asserting the number each
        operation returned rather than a separate read — because a separate
        read is precisely the arrangement this design rejects, and a test
        that used one would pass against the broken version.

        `1` on the way up and `0` on the way down are the two values the
        lifecycle branches on: the first marks a player online and the
        second marks them offline.
        """
        player_id = generate_uuid7()
        connections = [generate_uuid7() for _ in range(3)]

        opened = [
            await registry.register(
                player_id, connection_id, node_id=NODE_A, ttl_seconds=TTL_SECONDS
            )
            for connection_id in connections
        ]

        # `ZADD` on an existing member **rescores rather than appending**,
        # which is what lets the heartbeat reuse `register` to revive a
        # lapsed entry without inflating the count.
        repeated = await registry.register(
            player_id, connections[0], node_id=NODE_A, ttl_seconds=TTL_SECONDS
        )

        closed = [
            await registry.unregister(player_id, connection_id) for connection_id in connections
        ]
        # `ZREM` of an absent member removes nothing and still reports the
        # truth. Load-bearing rather than tidy: a second `unregister` that
        # reported a lower number would mark a player offline while another
        # tab is open, which is A64-016.1 §7's prohibition.
        repeated_removal = await registry.unregister(player_id, connections[0])

        assert opened == [1, 2, 3]
        assert repeated == 3
        assert closed == [2, 1, 0]
        assert repeated_removal == 0
        assert await registry.active_count(player_id) == 0

    async def test_the_registry_stores_and_resolves_the_node_that_holds_each_connection(
        self, registry: RedisConnectionRegistry
    ) -> None:
        """§12.4 — the whole reason `gwconn:v2:` exists.

        One player, three tabs, two nodes. v1 could record that the player
        had three connections and not where any of them was, so nothing
        could route a message to one; v2 packs the node into the member, and
        this asserts it survives the round trip through a sorted set and
        comes back attached to the right connection.

        The unregister at the end is the case that would break a naive
        implementation: cleanup knows the connection id and **not** the
        node, so a store that removed by reconstructing `connection|node`
        would fail to remove anything and leave a phantom route behind.
        """
        player_id = generate_uuid7()
        here, also_here, elsewhere = generate_uuid7(), generate_uuid7(), generate_uuid7()

        for connection_id, node_id in ((here, NODE_A), (also_here, NODE_A), (elsewhere, NODE_B)):
            await registry.register(
                player_id, connection_id, node_id=node_id, ttl_seconds=TTL_SECONDS
            )

        routes = await registry.routes_for(player_id)
        by_connection: dict[UUID, str] = {route.connection_id: route.node_id for route in routes}

        assert by_connection == {here: NODE_A, also_here: NODE_A, elsewhere: NODE_B}
        assert all(route.player_id == player_id for route in routes)
        assert await registry.node_for(player_id, elsewhere) == NODE_B

        # Cleanup by connection alone, as the lifecycle does it.
        assert await registry.unregister(player_id, elsewhere) == 2
        assert await registry.node_for(player_id, elsewhere) is None


@pytest_asyncio.fixture
async def rooms(contract_redis: Redis) -> RedisRoomMemberStore:
    return RedisRoomMemberStore(
        contract_redis, clock=MovableClock(NOW), progress_ttl_seconds=ROOM_TTL_SECONDS
    )


class TestTheRoomStore:
    """`gwroom:v1:` against real Redis — A64-016.3 §12.

    A64-016.2 shipped this keyspace with fake-based tests and recorded the
    gap in its own known-gaps list ("room membership and the reverse index
    are asserted against a fake that models them"). §12 closes it.

    Two tests for eight properties, because §16 caps this task at eight and
    the six move behaviours take the rest. Each carries the assertions that
    belong to one **mechanism** — the sorted set, and the monotonic
    compare-and-set — rather than one per line of code.
    """

    async def test_membership_is_atomic_idempotent_and_per_connection(
        self, rooms: RedisRoomMemberStore, contract_redis: Redis
    ) -> None:
        """§12's membership half: join, duplicate join, removal, one
        connection leaving, and the empty-room TTL.

        Every assertion is on the value the **write returned**, not on a
        separate read. That is the property a fake cannot have: the member
        set comes back from inside the same `MULTI`/`EXEC` that added the
        member, so "are both players here now" is a fact about the state
        this join produced rather than one the opponent's join may already
        have changed.

        The per-connection assertion is the one §12 names twice and the one
        a store keyed on the player alone would fail: two tabs, one leaves,
        the other stays. That is what makes a member the `(player,
        connection)` pair.

        **What this store deliberately cannot do** is refuse a
        non-participant. It has no roster and no way to get one — membership
        is `GameRoomService`'s rule, checked against `game.public` before
        any of these are called. §12 asks that a non-participant "cannot be
        inserted through the public store contract"; the honest reading is
        that the contract has no participant concept at all, which is
        asserted at the service level in
        `tests/unit/test_gateway_connection.py`.
        """
        match_id = generate_uuid7()
        player, opponent = generate_uuid7(), generate_uuid7()
        first_tab = RoomMember(player_id=player, connection_id=generate_uuid7())
        second_tab = RoomMember(player_id=player, connection_id=generate_uuid7())
        theirs = RoomMember(player_id=opponent, connection_id=generate_uuid7())

        after_first = await rooms.join(match_id, first_tab, ttl_seconds=ROOM_TTL_SECONDS)
        after_second = await rooms.join(match_id, second_tab, ttl_seconds=ROOM_TTL_SECONDS)
        after_duplicate = await rooms.join(match_id, second_tab, ttl_seconds=ROOM_TTL_SECONDS)
        after_opponent = await rooms.join(match_id, theirs, ttl_seconds=ROOM_TTL_SECONDS)

        assert list(after_first) == [first_tab]
        assert len(after_second) == 2
        # `ZADD` rescores rather than appending — the duplicate is not a
        # third member.
        assert len(after_duplicate) == 2
        assert len(after_opponent) == 3

        remaining = await rooms.leave(match_id, second_tab)
        assert set(remaining) == {first_tab, theirs}
        # One tab leaving does not take the player's other one.
        assert first_tab in remaining

        # Idempotent: removing what is already gone reports the truth.
        assert set(await rooms.leave(match_id, second_tab)) == {first_tab, theirs}

        # The key carries an expiry, so a room nobody returns to is dropped
        # rather than swept — §8's "empty room expires after TTL".
        ttl = await contract_redis.ttl(f"gwroom:v1:{match_id}")
        assert 0 < ttl <= ROOM_TTL_SECONDS + 60

        for member in (first_tab, theirs):
            await rooms.leave(match_id, member)
        assert await rooms.members_of(match_id) == ()

    async def test_the_room_sequence_advances_atomically_and_never_goes_backwards(
        self, rooms: RedisRoomMemberStore, contract_redis: Redis
    ) -> None:
        """§12's sequence half, and §11's projection.

        The monotonic compare-and-set. A plain `HSET` would pass the first
        two assertions and fail the third — and under at-least-once fan-out
        an out-of-order delivery is the ordinary case rather than a rare
        race, so a room reporting an older ply would send a resynchronising
        client backwards.

        Twenty concurrent writers with descending plies is the shape that
        makes it real: read-then-write would let whichever finished last
        win, and here exactly the highest survives however they interleave.
        """
        match_id = generate_uuid7()

        assert await rooms.record_progress(match_id, ply=1, side_to_move="dark", fingerprint="fp-1")
        assert await rooms.record_progress(
            match_id, ply=2, side_to_move="light", fingerprint="fp-2"
        )

        # An older ply is refused rather than written, and so is a repeat of
        # the current one — a redelivery must be a no-op, not a rewrite.
        assert not await rooms.record_progress(
            match_id, ply=1, side_to_move="dark", fingerprint="stale"
        )
        assert not await rooms.record_progress(
            match_id, ply=2, side_to_move="light", fingerprint="repeat"
        )

        progress = await rooms.progress_of(match_id)
        assert progress is not None
        assert (progress.ply, progress.side_to_move, progress.fingerprint) == (
            2,
            "light",
            "fp-2",
        )

        # Concurrent, descending. Exactly one write survives and it is the
        # highest, whatever order Redis executes them in.
        await asyncio.gather(
            *(
                rooms.record_progress(
                    match_id, ply=ply, side_to_move="dark", fingerprint=f"fp-{ply}"
                )
                for ply in range(22, 2, -1)
            )
        )

        settled = await rooms.progress_of(match_id)
        assert settled is not None
        assert settled.ply == 22
        assert settled.fingerprint == "fp-22"

        ttl = await contract_redis.ttl(f"gwroomstate:v1:{match_id}")
        assert 0 < ttl <= ROOM_TTL_SECONDS
