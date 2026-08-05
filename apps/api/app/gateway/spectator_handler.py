"""`SpectatorHandler` — joining and leaving a live match's audience.
A64-016.7 §2, §4.

    authenticate  ->  eligibility  ->  subscribe  ->  safe snapshot

## Read-only is enforced by what a spectator is not given, not by a check

§4 lists what a spectator may not do — submit moves, accept match actions,
alter clocks, join as a participant — and the honest way to enforce that is
structurally rather than with four guards:

A spectator's subscription lives in `gwspec:v1:`, and **every** one of those
operations checks `gwroom:v1:` instead. `MoveSubmissionHandler` asks
`GameRoomService.is_attached`, which reads the room; the acceptance path is
HTTP and behind `CurrentUser`; the clock is written only by the move
transaction and the adjudication worker.

So a spectator submitting a move is refused with `not_in_room` by machinery
that predates spectating and knows nothing about it — which is a stronger
guarantee than a check somebody has to remember to add to the next handler.

## Why the snapshot is the same one participants get

§4 says a spectator may receive "a safe Match snapshot", and the public state
of a live game *is* the board — by the time anybody can watch, the position
is visible to both players and to anybody they show it to. A narrower
snapshot format would be a second thing to keep in step with the position for
no privacy gained.

What is genuinely participant-only is on the **event** stream rather than the
board: a draw offer, a takeback request, an opponent's disconnection notice.
None exists yet, and the seam that keeps them out is the recipient set being
built per event rather than per room — see `delivery.RoomBroadcaster` and the
spectator-safe filter in `moves.py`.
"""

import logging
from typing import Final
from uuid import UUID

from app.gateway.metrics import (
    SPECTATOR_JOINS,
    SPECTATOR_LEAVES,
    SPECTATOR_REJECTIONS,
    SpectatorLeaveReason,
)
from app.gateway.projections import spectator_snapshot_payload
from app.gateway.protocol import (
    GatewayErrorCode,
    GatewayMessage,
    error,
    spectator_joined,
    spectator_left,
)
from app.gateway.spectators import (
    SpectatorEligibility,
    SpectatorRefusal,
    SpectatorStore,
    SpectatorSubscription,
)
from app.modules.game.public import MatchSnapshotReader
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)

#: Which wire code each refusal becomes.
#:
#: A mapping rather than a branch, so a reason added to `SpectatorRefusal`
#: without a code fails at import rather than silently reaching a client as
#: an internal error — the same shape the move path's `_REJECTIONS` keeps.
_REFUSAL_CODES: Final[dict[SpectatorRefusal, GatewayErrorCode]] = {
    SpectatorRefusal.NOT_SPECTATABLE: GatewayErrorCode.NOT_SPECTATABLE,
    SpectatorRefusal.BLOCKED: GatewayErrorCode.SPECTATING_FORBIDDEN,
    SpectatorRefusal.IS_PARTICIPANT: GatewayErrorCode.SPECTATING_FORBIDDEN,
}


class SpectatorHandler:
    """Handles `spectator.join` and `spectator.leave`."""

    def __init__(
        self,
        *,
        snapshots: MatchSnapshotReader,
        policy: SpectatorEligibility,
        store: SpectatorStore,
        metrics: MetricsRecorder,
        subscription_ttl_seconds: int,
    ) -> None:
        self._snapshots = snapshots
        self._policy = policy
        self._store = store
        self._metrics = metrics
        self._subscription_ttl_seconds = subscription_ttl_seconds

    async def join(
        self, message: GatewayMessage, *, player_id: UUID, connection_id: UUID
    ) -> GatewayMessage:
        """One `spectator.join`. Returns the frame to send back.

        Never raises. A handler that raised would close the socket, which
        for somebody who merely asked to watch a game is an overreaction —
        and would take their *playing* connection with it, since it is the
        same socket (AD-11).
        """
        match_id = _match_id_of(message)
        if match_id is None:
            return error(
                GatewayErrorCode.MALFORMED_MESSAGE,
                request_id=message.request_id,
                channel=message.channel,
            )

        snapshot = await self._snapshots.snapshot_of(match_id)
        if snapshot is None:
            return self._refuse(SpectatorRefusal.NOT_SPECTATABLE, message, player_id=player_id)

        refusal = await self._policy.refusal_for(snapshot, player_id=player_id)
        if refusal is not None:
            return self._refuse(refusal, message, player_id=player_id)

        audience = await self._store.subscribe(
            match_id,
            SpectatorSubscription(player_id=player_id, connection_id=connection_id),
            ttl_seconds=self._subscription_ttl_seconds,
        )

        self._metrics.increment(SPECTATOR_JOINS)
        # No match id and no player id in the label (§7); both are in the
        # log, which has the retention and access controls for them.
        logger.info(
            "spectator_joined",
            extra={"user_id": str(player_id), "match_id": str(match_id), "audience": audience},
        )
        return spectator_joined(
            spectator_snapshot_payload(snapshot),
            audience=audience,
            request_id=message.request_id,
        )

    async def leave(
        self, message: GatewayMessage, *, player_id: UUID, connection_id: UUID
    ) -> GatewayMessage:
        """One `spectator.leave`. Idempotent.

        Answered with `spectator.left` whether or not the connection was
        watching, because a client that asked to stop and has stopped got
        what it asked for — the same rule `room.leave` keeps.
        """
        match_id = _match_id_of(message)
        if match_id is None:
            return error(
                GatewayErrorCode.MALFORMED_MESSAGE,
                request_id=message.request_id,
                channel=message.channel,
            )

        await self._store.unsubscribe(
            match_id, SpectatorSubscription(player_id=player_id, connection_id=connection_id)
        )
        self._metrics.increment(SPECTATOR_LEAVES, labels={"reason": SpectatorLeaveReason.CLIENT})
        return spectator_left(match_id=match_id, request_id=message.request_id)

    async def detach(self, *, player_id: UUID, connection_id: UUID) -> int:
        """Stops this connection watching everything — the disconnect path.

        Never raises: it runs from the connection lifecycle's cleanup, on
        every path including one already handling a failure. A subscription
        left behind lapses on its own TTL, which is the backstop this design
        already has for a node that died.
        """
        subscription = SpectatorSubscription(player_id=player_id, connection_id=connection_id)
        try:
            left = await self._store.unsubscribe_all(subscription)
        except Exception as exc:  # noqa: BLE001 — cleanup must not escalate
            logger.error(
                "spectator_detach_failed",
                extra={"user_id": str(player_id), "error": type(exc).__name__},
                exc_info=exc,
            )
            return 0

        if left:
            self._metrics.increment(
                SPECTATOR_LEAVES,
                labels={"reason": SpectatorLeaveReason.DISCONNECT},
                by=len(left),
            )
        return len(left)

    def _refuse(
        self, refusal: SpectatorRefusal, message: GatewayMessage, *, player_id: UUID
    ) -> GatewayMessage:
        self._metrics.increment(SPECTATOR_REJECTIONS, labels={"reason": refusal})
        logger.info(
            "spectator_refused",
            extra={"user_id": str(player_id), "reason": refusal.value},
        )
        return error(
            _REFUSAL_CODES[refusal],
            request_id=message.request_id,
            channel=message.channel,
        )


def _match_id_of(message: GatewayMessage) -> UUID | None:
    """The match a spectator frame names, or `None`.

    **No player id**, and the frame has no field for one: the viewer is the
    socket's redeemed ticket, so a client cannot watch on somebody else's
    behalf or claim an identity that would pass a block check.
    """
    raw = message.payload.get("match_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


__all__ = ["SpectatorHandler"]
