"""The two implementations of `users.public.PresenceProvider` and
`users.public.PresenceRecorder` — A64-012.7.

    RedisPresenceProvider   the real one: one key per player, with a TTL
    NoPresenceProvider      nobody's presence is known

The choice is made once per request in the composition root
(`profiles.presentation.dependencies`) from `PresenceSettings.enabled`, and
it is logged there, because that is the only place that knows a *choice* was
made — neither class below can tell you what it was chosen instead of. That
is the arrangement `statistics_providers.py` already uses, for the same
reason.

## What the fallback is for

An operational kill switch, the same shape as `RateLimitSettings.enabled`
and `StatisticsSettings.enabled`: a deployment whose presence instance is
being replaced, resized or is simply unhealthy sets `PRESENCE_ENABLED=false`
and keeps serving profiles.

Unlike the statistics fallback, this one costs almost nothing, and it is
worth saying why rather than leaving it to be inferred. A blank statistics
record is a *lie* — indistinguishable from a genuine beginner's. Unknown
presence is not: `null` is what a profile already reports for a player who
is offline, who has hidden their presence, or whose window has expired, so a
deployment running on the fallback degrades to the answer most players get
anyway. That is why `NoPresenceProvider` is a legitimate degradation and not
merely a stub.

## Why the Redis adapter never raises

`PresenceProvider.presence_for` and `PresenceRecorder.record_presence` both
promise it, and this is where the promise is kept. Every call is bounded by
`PresenceSettings.redis_timeout_ms` and every exception below it is caught,
because the two failure modes have to behave identically: a Redis that is
*down* and a Redis that is *slow* are the same event to somebody waiting on
a profile page, and only the timeout catches the second.

There is no fail-open/fail-closed switch here, unlike the rate limiter,
because there is no defensible closed position. Presence is decoration on a
profile; refusing to render `GET /profiles/{username}` because an indicator
could not be computed would convert a cosmetic defect (system-design.md
§626) into an outage of the platform's highest-volume public read.
"""

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.config.settings import PresenceSettings
from app.core.clock import Clock
from app.modules.users.domain.presence import DeviceType, LapsedPresence, Presence
from app.modules.users.infrastructure.presence.keys import (
    FIELD_DEVICE_TYPE,
    FIELD_LAST_SEEN,
    FIELD_ONLINE,
    FIELD_SESSION_ID,
    presence_key,
    roster_key,
)

logger = logging.getLogger(__name__)


class RedisPresenceProvider:
    """Presence in Redis: one key per player, one command per operation.

    Constructed per request (the client it wraps is itself a process-lifetime
    pool, so this costs three attribute assignments) — see
    `app/api/deps.py:get_redis_pools`.

    ## Multi-node, by having nothing to coordinate

    Every gateway node writes the same key for a given player and every API
    node reads it. There is no registry of which node owns whom, no
    fan-out on write and no lock:

      - **Writes are last-writer-wins**, and that is correct rather than
        merely tolerable. Two observations of the same player seconds apart
        differ only in which session saw them; keeping the later one is the
        answer presence exists to give.
      - **The TTL is the liveness protocol.** A node that dies mid-session
        cannot clean up after itself, so nothing depends on it doing so —
        the record lapses and the player goes quiet. That is the property
        that makes a node failure a few seconds of staleness instead of a
        population of players permanently marked online.
      - **Reads are stateless.** An API node needs no membership view, which
        is what lets the read path scale independently of the socket tier.

    What this design deliberately cannot do is represent a player connected
    from two devices — the second connect overwrites the first, and the
    disconnect of either marks the player offline. A64-012.7 excludes
    multiple devices; `keys.py` records why that is a `v2` keyspace rather
    than a wider value here.
    """

    def __init__(self, redis: Redis, *, settings: PresenceSettings, clock: Clock) -> None:
        self._redis = redis
        self._settings = settings
        self._clock = clock

    async def presence_for(self, player_id: UUID) -> Presence | None:
        """The stored record, or `None` when there is nothing to report.

        `None` for an expired window, a player never recorded, a malformed
        value and an unreachable store alike — see `PresenceProvider` on why
        collapsing those is the requirement rather than a shortcut.
        """
        try:
            raw = await asyncio.wait_for(
                self._redis.get(presence_key(player_id)),
                timeout=self._settings.redis_timeout_ms / 1000,
            )
        except Exception as error:  # noqa: BLE001 — every failure is one outcome here
            # WARNING rather than ERROR: for as long as this fires, profiles
            # render without an online indicator and nothing else is wrong.
            # An operator wants to know; nobody should be paged (services.md
            # §7.1). Contrast `rate_limit_unavailable`, which is ERROR
            # because it means six endpoints are running unprotected.
            #
            # **No key.** A64-012.7: never log Redis keys. `user_id` is the
            # same fact in the form every other log line on the platform
            # already carries it in.
            logger.warning(
                "presence_unavailable",
                extra={"user_id": str(player_id), "error": type(error).__name__},
                exc_info=error,
            )
            return None

        if raw is None:
            return None

        return self._decode(raw, player_id)

    async def presence_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, Presence]:
        """A page of players in one `MGET` — A64-013.1.

        The read `presence_for` does, for many keys, in one round trip.
        Twenty search results rendered through `presence_for` would be
        twenty round trips to draw an indicator (CLAUDE.md §10.4).

        `MGET` rather than a pipeline of `GET`s: it is a single command
        Redis answers atomically per key, it is one network turnaround
        regardless of page size, and — the part that matters on a cluster —
        it is the shape a future hash-tagged keyspace can keep working with.

        Absent, expired and undecodable keys are simply **not in the
        result**. `MGET` returns a positionally aligned array with `None`
        for a missing key, and the zip below drops those, so a caller's
        `.get(id)` yields the same `None` `presence_for` would.

        Never raises, exactly as the single read does not. A whole page
        degrades to unknown together rather than one search failing.
        """
        if not player_ids:
            # No command at all for an empty page — the ordinary outcome of
            # a search nobody matched, and a zero-key `MGET` is a round trip
            # that can only return nothing.
            return {}

        keys = [presence_key(player_id) for player_id in player_ids]
        try:
            raw_values = await asyncio.wait_for(
                self._redis.mget(keys),
                timeout=self._settings.redis_timeout_ms / 1000,
            )
        except Exception as error:  # noqa: BLE001 — every failure is one outcome here
            # WARNING, and **one line for the whole page** rather than one
            # per player: a failing Redis would otherwise emit twenty
            # identical records per search and bury the incident in its own
            # noise (CLAUDE.md §8.8). The count is the useful part.
            logger.warning(
                "presence_unavailable",
                extra={"player_count": len(keys), "error": type(error).__name__},
                exc_info=error,
            )
            return {}

        found: dict[UUID, Presence] = {}
        for player_id, raw in zip(player_ids, raw_values, strict=True):
            if raw is None:
                continue
            presence = self._decode(raw, player_id)
            if presence is not None:
                found[player_id] = presence
        return found

    async def record_presence(
        self,
        player_id: UUID,
        *,
        is_online: bool,
        session_id: str | None = None,
        device_type: DeviceType | None = None,
    ) -> None:
        """Writes the whole record and (re)sets its expiry, in one command.

        `SET key value PX ttl` rather than a write followed by an expire:
        there is no instant at which the key exists without a TTL, so no
        sequence of crashes can leave a player marked online forever. That
        is the failure this whole design is arranged around — see `keys.py`
        on why the record is a string rather than a hash.

        The timestamp comes from the injected `Clock` (AD-07), never from
        Redis's own `TIME` and never from `datetime.now`: the value ends up
        on a profile, and a test asserting "last seen" against a fixed clock
        should not have to sleep.
        """
        payload: dict[str, Any] = {
            FIELD_ONLINE: is_online,
            FIELD_LAST_SEEN: self._clock.now().isoformat(),
        }
        # Omitted rather than written as `null`, so the stored value stays
        # the size of what was actually observed. Absent and null decode
        # identically below, so nothing downstream has to know which a
        # writer chose.
        if session_id is not None:
            payload[FIELD_SESSION_ID] = session_id
        if device_type is not None:
            payload[FIELD_DEVICE_TYPE] = device_type.value

        try:
            await asyncio.wait_for(
                self._write(player_id, payload, is_online=is_online),
                timeout=self._settings.redis_timeout_ms / 1000,
            )
        except Exception as error:  # noqa: BLE001 — a lost observation is not a failure
            # Swallowed on purpose, and the promise is on the port: a
            # gateway must not drop a socket because a presence write timed
            # out. The next observation writes the whole record anyway, so
            # one lost write self-heals within the refresh interval.
            logger.warning(
                "presence_write_failed",
                extra={"user_id": str(player_id), "error": type(error).__name__},
                exc_info=error,
            )

    async def _write(self, player_id: UUID, payload: dict[str, Any], *, is_online: bool) -> None:
        """The record and the roster entry, in one round trip — A64-013.8.

        A pipeline rather than two awaits: the roster is written on every
        observation, so a second round trip here would double the Redis cost
        of the platform's most frequent write.

        **The record is written first and the roster second**, and the order
        is the failure model. The record is the fact; the roster is a derived
        index whose only consumer is the sweeper. A pipeline that failed
        after the `SET` leaves a player correctly online with no roster
        entry — one missed *notification* when they leave, which is exactly
        the pre-A64-013.8 behaviour. The reverse ordering would leave a
        roster entry for a player who was never recorded, and the sweeper
        would announce a departure that never happened.

        `transaction=False`: these are two independent commands and neither
        reads the other, so `MULTI`/`EXEC` would buy atomicity nothing needs
        and cost a round trip's worth of blocking on the server.

        The roster score is the **expiry instant in milliseconds**, which is
        what makes `ZRANGEBYSCORE 0 now` mean "these windows have closed".
        `ZADD` overwrites an existing member's score, so a refresh moves the
        deadline rather than adding a duplicate — and an explicit offline
        removes the member outright, because a player who said they were
        leaving needs no sweeping.
        """
        pipeline = self._redis.pipeline(transaction=False)
        pipeline.set(presence_key(player_id), json.dumps(payload), px=self._settings.ttl_ms)

        if is_online:
            expires_at_ms = _to_millis(self._clock.now()) + self._settings.ttl_ms
            pipeline.zadd(roster_key(), {str(player_id): expires_at_ms})
        else:
            pipeline.zrem(roster_key(), str(player_id))

        await pipeline.execute()

    async def lapsed(self, *, now: datetime, limit: int) -> Sequence[LapsedPresence]:
        """Players whose window closed and whom nothing observed leaving.

        One `ZRANGEBYSCORE ... LIMIT 0 <limit>`, oldest lapse first, so a
        backlog drains in deadline order rather than in whatever order Redis
        happens to hold members.

        **Bounded on every axis**: the command carries a limit, the set holds
        one member per online player, and the caller ticks on a timer. There
        is no arrangement in which this reads an unbounded amount
        (CLAUDE.md §10.5).

        Returns `()` rather than raising when Redis is unreachable — the
        sweeper is a background job whose failure must not escalate, and the
        entries are still there for the next tick. Logged at `WARNING`, not
        `DEBUG`: unlike a presence read this one going quiet means departures
        are silently not being announced.
        """
        try:
            members = await asyncio.wait_for(
                self._redis.zrangebyscore(
                    roster_key(), min=0, max=_to_millis(now), start=0, num=limit, withscores=True
                ),
                timeout=self._settings.redis_timeout_ms / 1000,
            )
        except Exception as error:  # noqa: BLE001 — a background sweep must not escalate
            logger.warning(
                "presence_roster_read_failed",
                extra={"error": type(error).__name__},
                exc_info=error,
            )
            return ()

        lapsed: list[LapsedPresence] = []
        for member, score in members:
            player_id = _parse_player_id(member)
            if player_id is None:
                # Something other than this code wrote the roster. Dropped
                # rather than decoded, and the next `forget` cannot remove it
                # because it never became a `UUID` — so it is logged, once
                # per sweep, as the data-integrity event it is.
                logger.warning("presence_roster_malformed")
                continue
            lapsed.append(LapsedPresence(player_id=player_id, lapsed_at=_from_millis(float(score))))
        return lapsed

    async def forget(self, player_ids: Sequence[UUID]) -> None:
        """Drops these players from the roster. One `ZREM`, whatever the size.

        Never raises: a member left behind is swept again next tick, and the
        consumer's own idempotency is what makes that safe. See
        `PresenceSweeper` on why this runs *after* the events are committed.
        """
        if not player_ids:
            return

        try:
            await asyncio.wait_for(
                self._redis.zrem(roster_key(), *[str(player_id) for player_id in player_ids]),
                timeout=self._settings.redis_timeout_ms / 1000,
            )
        except Exception as error:  # noqa: BLE001 — a stale member is re-swept, not lost
            logger.warning(
                "presence_roster_forget_failed",
                extra={"player_count": len(player_ids), "error": type(error).__name__},
                exc_info=error,
            )

    def _decode(self, raw: bytes | str, player_id: UUID) -> Presence | None:
        """Stored value -> `Presence`, or `None` if it cannot be trusted.

        **Strict about the two fields that carry meaning, tolerant about the
        two that do not.** A record whose `online` is not a boolean or whose
        `last_seen` is not an instant is not a presence reading at all, and
        inventing a default would publish a fact nobody observed — so the
        whole record is discarded, which is the fail-safe direction on a
        field governed by a privacy flag.

        `session_id` and `device_type` are best-effort by contrast: an
        unrecognised device type from a newer gateway reads as `None` rather
        than discarding an otherwise valid record, because a rolling deploy
        must not make every profile stop showing presence.

        Never raises. This is reached from a public read path, and a
        hand-edited key in a debugging session must not 500 a profile.
        """
        try:
            document = json.loads(raw)
        except ValueError:
            self._malformed(player_id, "not_json")
            return None

        if not isinstance(document, dict):
            self._malformed(player_id, "not_an_object")
            return None

        is_online = document.get(FIELD_ONLINE)
        if not isinstance(is_online, bool):
            self._malformed(player_id, FIELD_ONLINE)
            return None

        last_seen = _parse_instant(document.get(FIELD_LAST_SEEN))
        if last_seen is None:
            self._malformed(player_id, FIELD_LAST_SEEN)
            return None

        session_id = document.get(FIELD_SESSION_ID)
        return Presence(
            is_online=is_online,
            last_seen=last_seen,
            session_id=session_id if isinstance(session_id, str) else None,
            device_type=_parse_device_type(document.get(FIELD_DEVICE_TYPE)),
        )

    @staticmethod
    def _malformed(player_id: UUID, field: str) -> None:
        """One place to log an undecodable record, so every rejection above
        is visible and none of them is silent (CLAUDE.md §2.7).

        Logs the *field name*, never the value: a stored record carries a
        session identifier, and a log line quoting a corrupt payload would
        put it somewhere with broader read access than Redis (services.md
        §8.5).
        """
        logger.warning(
            "presence_record_malformed",
            extra={"user_id": str(player_id), "field": field},
        )
        return None


class NoPresenceProvider:
    """Nobody's presence is known — the fallback.

    Reads report `None`, which is the same answer the Redis adapter gives
    for a player whose window has expired, so a deployment running on this
    is serving a value clients already handle rather than a special case
    they do not.

    Writes are accepted and discarded. Silently, and deliberately: this is
    what a gateway holds when presence is switched off, and a recorder that
    raised would turn a kill switch for a cosmetic feature into a broken
    connect path. The `WARNING` at selection in the composition root is
    where "presence is currently off" is made visible; a log line here would
    fire once per observation per online player, which is the definition of
    noise (CLAUDE.md §8.8).

    **Depends on nothing.** No Redis client, no settings, no clock — a
    fallback that imported the thing it replaces would fail for exactly the
    reasons it exists. Stateless and infallible, and it ignores every
    argument, which is the honest signature when the answer is the same for
    everyone.
    """

    async def presence_for(self, player_id: UUID) -> Presence | None:
        return None

    async def presence_for_many(self, player_ids: Sequence[UUID]) -> Mapping[UUID, Presence]:
        """An empty mapping — nobody is observed, so nobody has an entry.

        The same answer the Redis adapter gives for a page of players whose
        windows have all expired, which is what keeps this a legitimate
        degradation rather than a special case a client has to know about.
        """
        return {}

    async def record_presence(
        self,
        player_id: UUID,
        *,
        is_online: bool,
        session_id: str | None = None,
        device_type: DeviceType | None = None,
    ) -> None:
        return None

    async def lapsed(self, *, now: datetime, limit: int) -> Sequence[LapsedPresence]:
        """Nobody lapses, because nobody was ever recorded.

        The sweeper therefore ticks and finds nothing, which is the correct
        behaviour for a deployment with presence switched off — and is why
        the sweeper needs no kill switch of its own."""
        return ()

    async def forget(self, player_ids: Sequence[UUID]) -> None:
        return None


def _parse_instant(value: object) -> datetime | None:
    """An ISO-8601 string -> a timezone-aware UTC instant, or `None`.

    Normalised to UTC rather than trusted as written, per DM-14: a writer
    that recorded `+03:00` and a reader that compared it against a naive
    "now" is a bug that only appears for players in one part of the world.
    A value with no offset is rejected rather than assumed to be UTC —
    guessing here would silently shift a timestamp by hours.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _parse_device_type(value: object) -> DeviceType | None:
    """A stored device name -> a member, or `None` for anything else.

    Tolerant on purpose — see `DeviceType`. A newer gateway writing a fourth
    device must not stop an older API node from rendering presence, and the
    field is not published, so an unrecognised value costs nothing.
    """
    if not isinstance(value, str):
        return None
    try:
        return DeviceType(value)
    except ValueError:
        return None


def _to_millis(instant: datetime) -> int:
    """A UTC instant as epoch milliseconds — the roster's score type.

    Milliseconds rather than seconds because `PRESENCE_TTL_SECONDS` is
    expressed in `ttl_ms` internally and a sweep comparing a second-truncated
    deadline against a millisecond one would sweep a player up to a second
    early. Integers rather than floats because a score that carries sub-
    millisecond noise makes two equal deadlines compare unequal.
    """
    return int(instant.timestamp() * 1000)


def _from_millis(score: float) -> datetime:
    """A roster score back to the instant it encodes, in UTC (DM-14)."""
    return datetime.fromtimestamp(score / 1000, tz=UTC)


def _parse_player_id(member: bytes | str) -> UUID | None:
    """A roster member back to a player id, or `None` if it is not one.

    Tolerant for the reason `_decode` is: the roster is a shared keyspace,
    and the safe response to a value this code did not write is to ignore it
    rather than to fail a sweep over it.
    """
    raw = member.decode() if isinstance(member, bytes) else member
    try:
        return UUID(raw)
    except ValueError:
        return None
