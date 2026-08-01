"""`PresenceService` — the producer `PresenceRecorder` has been waiting for.

A64-012.7 built the presence *store* and its two ports, and said plainly
that nothing would write to it until "AD-09's gateway" existed. That gateway
is still not this task's to build (A64-013.6 excludes WebSockets), so the
question this module answers is: **what else knows a player is present?**

## Authentication is a presence signal, and it is the honest one

A player who has just proven their identity is at a keyboard. A player whose
client exchanged a refresh token thirty seconds ago still is. Those are the
two facts the platform already observes without any socket, and they are
what this service turns into presence:

    POST /auth/login      -> online
    POST /auth/refresh    -> online, and the TTL restarts
    POST /auth/logout-all -> offline

It is coarser than a socket and it is not a lie. `PresenceSettings.ttl_seconds`
is the whole reason it works: a player who closes the tab stops refreshing
and stops being online sixty seconds later, without anything having to
notice they left. That is the same liveness protocol a gateway would rely on
— the gateway simply refreshes more often.

## Why `POST /auth/logout` does *not* mark offline

Signing out of one device is not going offline. Presence is per **player**,
not per session (`presence:v1:<player_id>`), and a player signing out on a
laptop may still be signed in on a phone — marking them offline would
publish a falsehood to everybody watching, and the phone's next refresh
would flip it back.

`logout-all` is different: it revokes every session, so there is no device
left that could be present.

## What this service deliberately does not do

**It does not fan out.** Nothing is notified, nothing is published, no event
is emitted. A64-013.6 asks for the integration points and excludes realtime
delivery, and `PresenceAudienceService` in `friends` is the other half —
*who* would be told. Wiring the two together is A64-013.7's.

**It applies no privacy.** Whether a viewer may *see* presence is
`VisibilityLevel` and `ViewerRelationship`, applied by
`PublicProfileComposer` and nowhere else. This service records what
happened; the composer decides who may know.
"""

import logging
from uuid import UUID

from app.modules.users.domain.presence import DeviceType, Presence
from app.modules.users.public.ports import PresenceProvider, PresenceRecorder

logger = logging.getLogger(__name__)


class PresenceService:
    """Records that a player is present, or has gone.

    Holds both presence ports, which is the one place on the platform that
    does — and it is what makes this the *producer* rather than another
    consumer. `profiles` gets the reader alone, precisely so the module
    serving anonymous traffic cannot assert that somebody is online.
    """

    def __init__(self, *, recorder: PresenceRecorder, provider: PresenceProvider) -> None:
        self._recorder = recorder
        self._provider = provider

    async def mark_online(
        self,
        player_id: UUID,
        *,
        session_id: UUID | None = None,
        device_type: DeviceType | None = None,
    ) -> None:
        """Records that this player is present, and restarts their window.

        Called on sign-in and on every refresh. Idempotent by construction —
        the record is written whole each time, so calling it twice is
        calling it once with a later timestamp.

        `session_id` is the `auth` session, stored and **never published**:
        no response schema on the platform has a field it could land in.
        It is recorded because a live challenge is delivered to a
        *connection* rather than to an account, and the value that will
        route it is the one written here.

        **Never raises**, because `PresenceRecorder` does not: a sign-in
        must not fail because Redis was briefly unreachable. The cost of a
        lost write is that the player looks offline until their next
        refresh.
        """
        await self._recorder.record_presence(
            player_id,
            is_online=True,
            session_id=str(session_id) if session_id else None,
            device_type=device_type,
        )

        # DEBUG, not INFO. On a busy platform this fires on every sign-in
        # *and* every token refresh, which is a timer — at INFO it would be
        # the highest-volume line in the log and no signal at all
        # (services.md §7.1). The id only: when a player was online is
        # behaviour behind a privacy flag, and a log recording it would be
        # the sleep schedule `show_last_seen` exists to withhold.
        logger.debug("presence_online", extra={"user_id": str(player_id)})

    async def mark_offline(self, player_id: UUID) -> None:
        """Records that this player has gone.

        Called when every session is revoked. Writes a record rather than
        deleting one — `is_online=False` with a fresh `last_seen`, which is
        what makes "last seen four minutes ago" possible. Deleting would
        throw away the timestamp the record exists to carry.

        The record still expires on its own window, so "gone a long time
        ago" and "never here" converge on the same absence.
        """
        await self._recorder.record_presence(player_id, is_online=False)

        # INFO here and DEBUG above, deliberately: going offline is a
        # single event per session-set, while going online fires on every
        # refresh. The asymmetry is about volume, not importance.
        logger.info("presence_offline", extra={"user_id": str(player_id)})

    async def presence_of(self, player_id: UUID) -> Presence | None:
        """This player's own presence, unredacted.

        The read behind the owner's view on `GET /profile/me`, which
        `ProfileService.get_own_presence` already serves — this exists so
        that a caller holding *this* service does not have to reach for
        another one to verify what it just wrote.

        Applies no privacy, and must not: the only caller is the account
        holder, and a settings screen that hid a player's own presence from
        them would be a control nobody could verify they had set.
        """
        return await self._provider.presence_for(player_id)
