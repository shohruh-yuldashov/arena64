"""`PresenceSweeper` — the missing `offline` transitions, A64-013.8.

A64-013.7 recorded the gap in its own recommendations: "a player whose window
expires unobserved generates no `PresenceOffline`, so friends see them online
until the TTL. It is the one presence edge the platform cannot currently see."

This closes it, and the shape of the fix is decided by one fact: **an expired
key is gone.** There is nothing to scan for, no notification to subscribe to
that this platform is willing to depend on (`LISTEN`/`NOTIFY`-style
fire-and-forget is ruled out by database.md §1471's reasoning, and Redis
keyspace events have the same "silently never arrives" failure mode). So the
sweeper reads the one thing that outlives the lapse: the roster, which records
who is *due* to expire and when.

## The tick

    1. lapsed(now, limit)          the roster, oldest deadline first
    2. presence_for_many(...)      drop anyone who is back — see below
    3. publish PresenceOffline     one transaction for the whole batch
    4. forget(...)                 after the commit

**Step 2 is not defensive padding.** Between a player's deadline passing and
this tick running, they may have signed in again: their record exists and
their roster score has moved into the future, but this tick already read the
old score. Emitting then would announce a departure for somebody who is
present, which is worse than the gap being closed. One batched `MGET` removes
the race for the whole batch, and reuses the existing read rather than adding
one (A64-013.8: "reuse existing Presence infrastructure").

**Step 4 runs after the commit**, in the same direction and for the same
reason as the social graph cache's invalidation: a crash between the two
re-sweeps those players next tick and emits a second event, which is
at-least-once and is what the outbox's whole posture already assumes. The
reverse — forget first — would lose the transition entirely on a crash, and a
lost departure is the bug this class exists to fix.

## What it deliberately does not do

**It writes no presence record.** The window closed; writing `is_online=false`
would create a *new* key with a fresh TTL and a `last_seen` of now — a record
claiming the player was here at sweep time, which is a fabrication. The absence
already reads correctly as "unknown" through every existing path.

**It has no kill switch of its own.** With `PRESENCE_ENABLED=false` the roster
is `NoPresenceProvider`'s, which is always empty, so the sweeper ticks and
finds nothing. One switch, one meaning.
"""

import logging
from dataclasses import dataclass

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.users.public import PresenceOffline, PresenceProvider, PresenceRoster
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one `sweep_once` did. Returned rather than only logged, so a test
    asserts on the outcome and the worker logs it once."""

    lapsed: int
    """Roster members whose deadline had passed."""

    emitted: int
    """Of those, the ones genuinely gone — `lapsed - emitted` came back."""

    @property
    def is_idle(self) -> bool:
        return self.lapsed == 0


class PresenceSweeper:
    """Emits the `offline` transitions nobody was there to observe.

    Holds three presence-side collaborators and a publisher. Notably **not**
    a `PresenceRecorder`: this class must not be able to write presence, and
    the port it does not hold is what guarantees it.
    """

    def __init__(
        self,
        *,
        roster: PresenceRoster,
        presence: PresenceProvider,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
        batch_size: int,
    ) -> None:
        self._roster = roster
        self._presence = presence
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._batch_size = batch_size

    async def sweep_once(self) -> SweepResult:
        """One batch of lapsed players. Never raises.

        A sweeper that propagated an exception would stop the loop calling
        it, which turns one bad tick into "no departure is ever announced
        again" — the same argument `OutboxRelay.run_once` makes.
        """
        now = self._clock.now()
        lapsed = await self._roster.lapsed(now=now, limit=self._batch_size)
        if not lapsed:
            return SweepResult(lapsed=0, emitted=0)

        # One read for the whole batch. A player with a live record came back
        # between their deadline and this tick, and must not be announced as
        # gone — but they *are* removed from this tick's roster clean-up
        # below, because their sign-in already rewrote their score.
        live = await self._presence.presence_for_many([entry.player_id for entry in lapsed])
        departed = [entry for entry in lapsed if entry.player_id not in live]

        try:
            async with self._unit_of_work:
                for entry in departed:
                    await self._events.publish(
                        PresenceOffline(occurred_at=entry.lapsed_at, player_id=entry.player_id)
                    )
                await self._unit_of_work.commit()
        except Exception as error:  # noqa: BLE001 — a background sweep must not escalate
            # Nothing is forgotten: the roster still holds every one of them,
            # so the next tick tries again. `ERROR` because a sweep that
            # cannot record means departures are silently not announced.
            logger.error(
                "presence_sweep_failed",
                extra={"lapsed": len(lapsed), "error": type(error).__name__},
                exc_info=error,
            )
            return SweepResult(lapsed=len(lapsed), emitted=0)

        # After the commit — see this module's docstring on why a duplicate
        # is the acceptable failure here and a loss is not.
        await self._roster.forget([entry.player_id for entry in lapsed])

        logger.info(
            "presence_sweep_completed",
            extra={"lapsed": len(lapsed), "emitted": len(departed)},
        )
        return SweepResult(lapsed=len(lapsed), emitted=len(departed))
