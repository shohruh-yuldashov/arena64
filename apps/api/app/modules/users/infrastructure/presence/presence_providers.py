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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.config.settings import PresenceSettings
from app.core.clock import Clock
from app.modules.users.domain.presence import DeviceType, Presence
from app.modules.users.infrastructure.presence.keys import (
    FIELD_DEVICE_TYPE,
    FIELD_LAST_SEEN,
    FIELD_ONLINE,
    FIELD_SESSION_ID,
    presence_key,
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
                self._redis.set(
                    presence_key(player_id),
                    json.dumps(payload),
                    px=self._settings.ttl_ms,
                ),
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

    async def record_presence(
        self,
        player_id: UUID,
        *,
        is_online: bool,
        session_id: str | None = None,
        device_type: DeviceType | None = None,
    ) -> None:
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
