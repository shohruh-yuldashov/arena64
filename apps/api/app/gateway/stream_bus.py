"""`RedisStreamGatewayBus` — the production transport. A64-016.5 §9.

A64-016.4 built `GatewayBus` and shipped `InProcessGatewayBus` behind it,
recording exactly what that cost: *"a fan-out to another node is queued in
memory and never crosses. Single-node is the only supported topology."* This
is the adapter that makes multi-node real, and it goes where A64-016.4 said
it would.

## The keyspace

    gwbus:v1:<node_id>   ->  stream, MAXLEN-capped
                             entry fields: node_id, connection_ids, frame

One stream per **destination node**, not one shared stream with filtering.
A shared stream would make every node read every other node's traffic and
discard most of it, which is fan-out cost paid by everyone to serve one
recipient. A stream per node means a node reads only what was addressed to
it, which is also why `node_id` is carried *inside* the entry: a consumer can
verify the message was meant for it rather than trusting its own
subscription.

## Consumer groups, and why not a plain `XREAD`

`XREADGROUP` with one group per node and the node's own id as the consumer
name. Two reasons, and the second is the one that matters:

**Restart safety.** A plain `XREAD` from `$` loses everything published while
the node was starting; from a remembered id, the node has to persist that id
somewhere, which is a second piece of state to keep in step. A consumer group
remembers the position for it.

**Explicit acknowledgement.** §9 asks for "explicit acknowledgements or safe
consumer semantics". `XACK` after delivery means an entry a node read and
then died on stays pending and is redeliverable — at-least-once, which is
exactly the guarantee the frames need and already tolerate.

**Duplicate delivery is safe** because every frame carries a ply: a client
that sees the same ply twice ignores the second, and A64-016.3's room
projection refuses to move backwards. That is why this adapter does not
deduplicate and does not need to.

## Bounded, on both axes

    MAXLEN ~ GATEWAY_BUS_MAX_STREAM_LENGTH   entries per node
    TTL    ~ GATEWAY_BUS_STREAM_TTL_SECONDS  the key itself

The length cap is what makes a node that has gone away safe: its stream stops
growing and the oldest entries are dropped, which for realtime frames is the
correct loss — a client that missed a ply resynchronises (A64-016.6), and one
that missed the *newest* ply is looking at a stale board anyway.

The key TTL is the second half, and it is what the length cap alone does not
give: a node that never comes back leaves a capped-but-permanent stream, and
one key per node that ever existed is unbounded in the fleet's history rather
than in its size. Refreshed on every publish, so a live node's stream never
lapses.

`~` (approximate trimming) rather than exact, because exact `MAXLEN` forces
Redis to trim to a precise length on every write and the difference between
1000 and 1000-and-a-few entries is nothing anybody can observe.

## Which Redis role

`bus`. AD-03 assigns it to pub/sub fan-out and nothing has used it until now
— which is the point: cross-node realtime traffic must not compete for memory
or connections with live match positions (`live`) or with anything evictable
(`cache`).
"""

import logging
from collections.abc import Sequence
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.gateway.bus import BusMessage

logger = logging.getLogger(__name__)

#: Bumped when the *entry shape* changes — caching.md C-2.
KEY_VERSION: Final = "v1"

_KEY_PREFIX: Final = f"gwbus:{KEY_VERSION}:"

#: One group per node. The name is fixed rather than per consumer because
#: the group *is* the node — a second consumer in it would be a second
#: process claiming to be the same gateway, which cannot happen: a node id
#: identifies a process.
_GROUP: Final = "gateway"

#: The entry's fields. Named once so the writer and the reader cannot
#: disagree — a typo in one of a matched pair is a bug that only appears
#: against a real second node.
_NODE_FIELD: Final = "node_id"
_CONNECTIONS_FIELD: Final = "connection_ids"
_FRAME_FIELD: Final = "frame"

#: Separates connection ids inside one field.
#:
#: A stream entry's values are flat strings, so the list is joined. A comma
#: rather than JSON because the values are UUIDs — a fixed-shape list of
#: fixed-shape tokens, where JSON would cost an encode and a decode per
#: fan-out to express nothing extra.
_CONNECTION_SEPARATOR: Final = ","


class RedisStreamGatewayBus:
    """`GatewayBus` over Redis streams, on the `bus` role."""

    def __init__(
        self,
        redis: Redis,
        *,
        max_stream_length: int,
        stream_ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self._max_stream_length = max_stream_length
        self._stream_ttl_seconds = stream_ttl_seconds
        self._groups: set[str] = set()

    @staticmethod
    def _key(node_id: str) -> str:
        return f"{_KEY_PREFIX}{node_id}"

    async def publish(self, message: BusMessage) -> bool:
        """Appends one entry to the destination node's stream.

        **Never raises** — `GatewayBus`'s contract. The move this frame
        describes is already committed, so a transport failure must be
        reported rather than propagated: propagating would turn a delivery
        problem into a move that appears to have failed after it was made.

        `XADD` and `EXPIRE` are two commands rather than one, and that is
        the one place this adapter is not atomic. It does not need to be: a
        crash between them leaves an entry in a stream with the *previous*
        TTL, which is a key that expires slightly sooner than intended —
        and the next publish refreshes it.
        """
        try:
            key = self._key(message.node_id)
            await self._redis.xadd(
                key,
                {
                    _NODE_FIELD: message.node_id,
                    _CONNECTIONS_FIELD: _CONNECTION_SEPARATOR.join(message.connection_ids),
                    _FRAME_FIELD: message.frame,
                },
                maxlen=self._max_stream_length,
                approximate=True,
            )
            await self._redis.expire(key, self._stream_ttl_seconds)
        except Exception as exc:  # noqa: BLE001 — a transport must not fail a move
            logger.warning("gateway_stream_publish_failed", extra={"error": type(exc).__name__})
            return False

        return True

    async def consume(self, node_id: str, *, limit: int) -> Sequence[BusMessage]:
        """Up to `limit` entries addressed to this node, acknowledged.

        **Acknowledged after decoding, before returning.** The alternative —
        acknowledging after the caller has delivered — would need the caller
        to hand entry ids back, which is a second contract for a guarantee
        the frames do not need: they are idempotent by ply, so a redelivery
        is free and a *lost* delivery is what actually costs something.

        Never raises: a consumer that propagated would stop the read loop
        that called it, which turns one Redis blip into a node that stops
        receiving remote traffic until it is restarted.
        """
        try:
            await self._ensure_group(node_id)
            entries = await self._redis.xreadgroup(
                groupname=_GROUP,
                consumername=node_id,
                streams={self._key(node_id): ">"},
                count=limit,
            )
        except ResponseError as error:
            # `NOGROUP` — A64-028.5. The stream key carries a TTL refreshed
            # only on publish, so a node that receives no cross-node traffic
            # for that long loses its key *and the consumer group with it*.
            # `_ensure_group` had already recorded the group as created, so
            # it returned without doing anything and every subsequent read
            # failed the same way — **for ever, until the process
            # restarted**. A quiet node stopped receiving realtime frames
            # and said so only in a warning nobody was reading: 4812 of them
            # in one instance's log before a load run noticed.
            #
            # Forgetting the cache entry and asking again is the whole fix.
            # It is idempotent (`BUSYGROUP` is already tolerated) and costs
            # one extra round trip on a path that has just failed anyway.
            if "NOGROUP" not in str(error):
                logger.warning(
                    "gateway_stream_consume_failed", extra={"error": type(error).__name__}
                )
                return ()

            logger.info("gateway_stream_group_recreated", extra={"node": node_id})
            self._groups.discard(node_id)
            try:
                await self._ensure_group(node_id)
                entries = await self._redis.xreadgroup(
                    groupname=_GROUP,
                    consumername=node_id,
                    streams={self._key(node_id): ">"},
                    count=limit,
                )
            except Exception as retry_error:  # noqa: BLE001 — must not stop the loop
                logger.warning(
                    "gateway_stream_consume_failed",
                    extra={"error": type(retry_error).__name__},
                )
                return ()
        except Exception as exc:  # noqa: BLE001 — a consumer must not stop its loop
            logger.warning("gateway_stream_consume_failed", extra={"error": type(exc).__name__})
            return ()

        return await self._decode_all(node_id, entries)

    async def pending(self, node_id: str) -> int:
        """How many entries this node's stream holds. For an operator and a
        test; nothing in the flow branches on it."""
        try:
            return int(await self._redis.xlen(self._key(node_id)))
        except Exception:  # noqa: BLE001 — a diagnostic must not raise
            return 0

    async def _ensure_group(self, node_id: str) -> None:
        """Creates this node's consumer group, once per process.

        `MKSTREAM`, so the group can be created before anything has ever
        been published to the node — otherwise the first consume on a quiet
        node would fail on a key that does not exist yet.

        `BUSYGROUP` is swallowed rather than pre-checked: the group already
        existing is the ordinary case after the first call, and checking
        first would be a round trip on every consume to avoid an error that
        means "fine".
        """
        if node_id in self._groups:
            return

        try:
            await self._redis.xgroup_create(
                name=self._key(node_id), groupname=_GROUP, id="0", mkstream=True
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

        self._groups.add(node_id)

    async def _decode_all(self, node_id: str, entries: Any) -> Sequence[BusMessage]:
        """Every readable entry, acknowledged.

        An entry this build cannot parse is **acknowledged and dropped**,
        not left pending. It was written by a different build during a
        rolling deploy, and leaving it pending would make it redeliver on
        every consume forever — a poison entry that costs one undelivered
        frame becoming one that costs the node's whole read loop.
        """
        decoded: list[BusMessage] = []
        acknowledged: list[Any] = []

        for _stream, records in entries or ():
            for entry_id, fields in records:
                acknowledged.append(entry_id)
                message = _decode(node_id, fields)
                if message is not None:
                    decoded.append(message)

        if acknowledged:
            try:
                await self._redis.xack(self._key(node_id), _GROUP, *acknowledged)
            except Exception as exc:  # noqa: BLE001 — a redelivery is safe
                logger.warning("gateway_stream_ack_failed", extra={"error": type(exc).__name__})

        return tuple(decoded)


def _decode(node_id: str, fields: Any) -> BusMessage | None:
    """One stream entry as a message, or `None` if it cannot be read.

    Verifies the entry's own `node_id` against the reader's. A mismatch
    means a stream was written for somebody else — which cannot happen with
    a key per node and is checked anyway, because the failure it would
    otherwise produce is a frame delivered to the wrong player's socket.
    """
    values = {_as_text(key): _as_text(value) for key, value in (fields or {}).items()}

    frame = values.get(_FRAME_FIELD)
    written_for = values.get(_NODE_FIELD)
    if frame is None or written_for is None:
        logger.warning("gateway_stream_entry_malformed")
        return None

    if written_for != node_id:
        logger.error("gateway_stream_entry_misaddressed", extra={"consumer": node_id})
        return None

    raw_connections = values.get(_CONNECTIONS_FIELD, "")
    connection_ids = tuple(entry for entry in raw_connections.split(_CONNECTION_SEPARATOR) if entry)
    return BusMessage(node_id=written_for, connection_ids=connection_ids, frame=frame)


def _as_text(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


__all__ = ["KEY_VERSION", "RedisStreamGatewayBus"]
