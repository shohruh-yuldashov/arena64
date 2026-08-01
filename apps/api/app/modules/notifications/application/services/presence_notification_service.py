"""`PresenceNotificationService` — the application service A64-013.7 asks
for: "do NOT call [PresenceService] directly from controllers; create an
application service coordinating them".

`auth`'s three lifecycle routes hold this instead of `PresenceService`, and
what it adds to the thing it wraps is one decision: **is this an edge?**

    login    by a player who was offline    -> record + PresenceOnline
    login    by a player already online     -> record, no event
    refresh  (the common case, on a timer)  -> record, no event
    logout-all by an online player          -> record + PresenceOffline
    logout-all by an offline player         -> record, no event

A64-013.7: "presence notifications are emitted only on state transitions ...
do NOT emit events on repeated refreshes." A busy player refreshes a token
every few minutes for hours; without this check every one of those would be
an outbox row, a relay tick, an audience resolution and a fan-out to every
friend — for a state that did not change.

## The read that makes it possible

Edge detection needs the previous state, so this reads presence before
writing it: one `GET` in front of the `SET` that was already there. That is
a real cost on the sign-in path and it is the minimum — the alternative,
`SET ... GET` in one round trip, is a genuine improvement and is recorded in
A64-013.7's recommendations rather than taken here, because it means widening
the published `PresenceRecorder` port for an optimisation nothing has
measured.

**A failed read is treated as "was offline"**, so a Redis blip produces a
spurious `PresenceOnline` rather than a missed one. Deliberate, and in the
direction the platform can absorb: a duplicate "your friend is online" is
noise, while a missed one is a friend who silently never appears.

## The outbox write is not in the same transaction as the state change

AD-16's rule is that an event is written in the same transaction as the fact
that caused it. Presence's fact lives in **Redis**, which cannot enlist in a
PostgreSQL transaction — so this is the one producer on the platform where
that guarantee is unavailable rather than merely unimplemented.

What is done instead: the Redis write happens first, then the outbox row is
committed. A crash in between loses the event, not the presence. That
ordering is chosen because the reverse — event first — would announce a
transition that then failed to happen, and a phantom "online" is worse than
a missed one for a fact that self-heals: the next transition re-establishes
the truth, and the presence TTL bounds how long a missed offline can be
wrong.

This is the whole justification for `PresenceOnline` carrying no audience
and no rendered payload. The event is a *hint that something changed*; the
truth is re-read at delivery.
"""

import logging
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.notifications.application.ports import PresenceWriter
from app.modules.users.public import DeviceType, PresenceOffline, PresenceOnline
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class PresenceNotificationService:
    """Records presence and emits the edges.

    Holds the presence port, a publisher, a unit of work and a clock — and
    notably **not** `PresenceAudience`. Who is told is resolved at delivery
    by `SocialNotificationDispatcher`, because a block placed between here
    and there is exactly the one that matters (A64-013.7: "do NOT trust
    enqueue-time state").
    """

    def __init__(
        self,
        *,
        presence: PresenceWriter,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._presence = presence
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def record_online(
        self,
        player_id: UUID,
        *,
        session_id: UUID | None = None,
        device_type: DeviceType | None = None,
    ) -> None:
        """Marks the player present, and emits `PresenceOnline` on the edge.

        Called by `POST /auth/login` and `POST /auth/refresh`. Never raises:
        the presence write cannot (its port forbids it), and the event write
        is guarded below — a sign-in must not fail because the outbox did.
        """
        was_online = await self._was_online(player_id)
        await self._presence.mark_online(player_id, session_id=session_id, device_type=device_type)

        if was_online:
            # The refresh case, and by volume the overwhelming majority.
            # DEBUG rather than nothing: "the transition check is running and
            # suppressing" is what an operator wants to see when asking why a
            # presence notification did not arrive.
            logger.debug("presence_transition_suppressed", extra={"user_id": str(player_id)})
            return

        await self._emit(PresenceOnline(occurred_at=self._clock.now(), player_id=player_id))

    async def record_offline(self, player_id: UUID) -> None:
        """Marks the player gone, and emits `PresenceOffline` on the edge.

        Called by `POST /auth/logout-all` — the only endpoint that leaves no
        device able to be present. `POST /auth/logout` calls nothing here,
        for the reason its own docstring gives.
        """
        was_online = await self._was_online(player_id)
        await self._presence.mark_offline(player_id)

        if not was_online:
            logger.debug("presence_transition_suppressed", extra={"user_id": str(player_id)})
            return

        await self._emit(PresenceOffline(occurred_at=self._clock.now(), player_id=player_id))

    async def _was_online(self, player_id: UUID) -> bool:
        """The previous edge state. `None` and an offline record are both
        "not here" — see `PresenceWriter.presence_of`."""
        previous = await self._presence.presence_of(player_id)
        return previous is not None and previous.is_online

    async def _emit(self, event: PresenceOnline | PresenceOffline) -> None:
        """Commits one outbox row in its own transaction.

        **Guarded**, unlike every other producer on this platform. A friends
        service publishing inside its own unit of work must let a failure
        roll the whole thing back, because the event and the state change are
        one fact. Here they are not — the state change already happened, in
        Redis, and cannot be undone by a PostgreSQL rollback — so a failure
        to record the event must not turn a successful sign-in into a `500`.

        `ERROR`, because a lost event is a lost notification and there is no
        retry: nothing recorded that it was owed. That is the cost of the
        deviation this module's docstring sets out, and it is logged as such
        rather than absorbed quietly.
        """
        try:
            async with self._unit_of_work:
                await self._events.publish(event)
                await self._unit_of_work.commit()
        except Exception as error:  # noqa: BLE001 — a sign-in must not fail for a notification
            logger.error(
                "presence_event_not_recorded",
                extra={
                    "event_type": type(event).event_type,
                    "user_id": str(event.player_id),
                    "error": type(error).__name__,
                },
                exc_info=error,
            )
