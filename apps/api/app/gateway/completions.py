"""`GatewayCompletionSink` — telling the players their game ended.
A64-031.A.

    game.match_completed  ->  [ this consumer ]  ->  game.completed on both sockets

## The gap this closes

Every way a match ends publishes `MatchCompleted`, and two of the three also
put a frame on the participants' sockets on the way out: a move that ends the
game rides `game.move.applied`, and a resignation or an accepted draw rides
`game.completed` from the command handler. Both of those are *gateway* paths,
so the players are right there.

**A flag is not.** `ClockAdjudicationService` runs on the worker, holds no
broadcaster and has no socket to write to — by design, because it must settle
a game whose players have both closed their tabs. So a match that timed out
told nobody: the clock hit zero, the worker completed the match correctly,
the rating moved, the notification went out, and the two browsers sat on a
board that never changed until somebody reloaded.

## Why an outbox consumer rather than a broadcaster on the worker

The worker could hold a `RoomBroadcaster` — the routing registry is
fleet-wide and the bus reaches every node, so it would work. It would also
put a socket concern inside clock adjudication, which is the one service on
this platform that must keep running when every socket is gone.

A consumer keeps that separation and buys the delivery guarantee the outbox
already provides: the event is durable before this runs, so a gateway that
was restarting when a game flagged retries on the next tick instead of losing
the frame.

## Every completion, not only a flag

This does not filter for `FLAG`, and the duplication is deliberate. A game
that ended by resignation now gets `game.completed` twice — once from the
command handler, once from here — and the client's reducer is idempotent for
exactly this reason: it sets a terminal result it already holds.

Filtering would mean this consumer knowing which terminations happen to have
a gateway in their call path, which is a coupling to *how* a game ended
rather than *that* it ended. One frame per completed game is a cost worth
paying to make "the players are told" true of every path rather than of two
out of three.

## Failure posture

Never raises and never reports a failure. The durable record is the match
row, which `game.snapshot` serves on the next resume, so a frame that could
not be delivered costs a client one reconnect — and a consumer that failed
the batch would hold up the rating and statistics entries beside it.
"""

import logging
from collections.abc import Sequence
from typing import Any, Final
from uuid import UUID

from app.gateway.delivery import RoomBroadcaster
from app.gateway.metrics import COMPLETION_PUSHES, CompletionPushOutcome
from app.gateway.protocol import game_completed
from app.gateway.spectators import SpectatorStore
from app.platform.metrics import MetricsRecorder
from app.platform.outbox.entry import OutboxEntry
from app.platform.outbox.ports import EventFailure

logger = logging.getLogger(__name__)

#: The event this consumes — `game.domain.events.MatchCompleted`.
MATCH_COMPLETED: Final = "game.match_completed"

#: Namespaced like `statistics.match_completed` and `rating.match_completed`,
#: so three consumers of one event keep three separate ledgers.
CONSUMER_NAME: Final = "gateway.match_completed"


class GatewayCompletionSink:
    """Puts a terminal result on the participants' sockets."""

    def __init__(
        self,
        *,
        broadcaster: RoomBroadcaster,
        spectators: SpectatorStore,
        metrics: MetricsRecorder,
    ) -> None:
        self._broadcaster = broadcaster
        self._spectators = spectators
        self._metrics = metrics

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        return event_type == MATCH_COMPLETED

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[EventFailure]:
        """One batch. Always reports no failures — see the module docstring."""
        for entry in entries:
            completion = _decoded(entry.payload)
            if completion is None:
                # Unprocessable rather than failed: a payload this cannot
                # read will not become readable, and retrying it would hold
                # the batch behind a frame nobody is waiting for.
                self._metrics.increment(
                    COMPLETION_PUSHES, labels={"outcome": CompletionPushOutcome.UNREADABLE}
                )
                logger.warning("gateway_completion_unreadable", extra={"entry_id": str(entry.id)})
                continue

            await self._push(completion)

        return ()

    async def _push(self, completion: "_Completion") -> None:
        try:
            report = await self._broadcaster.deliver(
                game_completed(
                    match_id=completion.match_id,
                    ply=completion.ply,
                    result=completion.result,
                ),
                recipients=completion.participants,
                spectators=await self._watching(completion.match_id),
            )
        except Exception as exc:  # noqa: BLE001 — a socket must not fail a batch
            self._metrics.increment(
                COMPLETION_PUSHES, labels={"outcome": CompletionPushOutcome.FAILED}
            )
            logger.warning(
                "gateway_completion_push_failed",
                extra={"match_id": str(completion.match_id), "error": type(exc).__name__},
            )
            return

        # `local + remote_nodes == 0` is the ordinary state of a game both
        # players left, not an error — the result is on the match row and
        # `game.snapshot` carries it whenever either of them comes back.
        delivered = report.local + report.remote_nodes > 0
        self._metrics.increment(
            COMPLETION_PUSHES,
            labels={
                "outcome": (
                    CompletionPushOutcome.DELIVERED if delivered else CompletionPushOutcome.NOBODY
                )
            },
        )

    async def _watching(self, match_id: UUID) -> Sequence[Any]:
        """Who is spectating, or nobody if that cannot be read.

        The same posture `commands.py` takes: an audience that could not be
        resolved must not stop the two people whose game it was from being
        told it is over.
        """
        try:
            return await self._spectators.routes_for(match_id)
        except Exception as exc:  # noqa: BLE001 — an audience must not fail a push
            logger.warning(
                "gateway_completion_spectators_failed",
                extra={"match_id": str(match_id), "error": type(exc).__name__},
            )
            return ()


class _Completion:
    """What this consumer needs from the event, and nothing else."""

    __slots__ = ("match_id", "participants", "ply", "result")

    def __init__(
        self,
        *,
        match_id: UUID,
        ply: int,
        result: dict[str, Any],
        participants: tuple[UUID, ...],
    ) -> None:
        self.match_id = match_id
        self.ply = ply
        self.result = result
        self.participants = participants


def _decoded(payload: dict[str, Any]) -> _Completion | None:
    """A completion payload as a frame and its recipients, or `None`.

    The participants come from the event's own seat summaries rather than
    from a repository read, which is what `SeatSummary` was made
    primitive-only for: this consumer runs in a process that may have no
    database session for `game` at all.
    """
    try:
        match_id = UUID(str(payload["match_id"]))
    except (KeyError, ValueError, TypeError):
        return None

    outcome = payload.get("outcome")
    reason = payload.get("termination_reason")
    if not isinstance(outcome, str) or not isinstance(reason, str):
        return None

    ply = payload.get("ply_number")
    if not isinstance(ply, int) or isinstance(ply, bool):
        return None

    participants = tuple(
        seat
        for seat in (_player(payload.get("light")), _player(payload.get("dark")))
        if seat is not None
    )
    if not participants:
        # A match created before seat summaries existed. There is nobody to
        # address, and inventing a recipient is worse than sending nothing.
        return None

    winner = payload.get("winner")
    return _Completion(
        match_id=match_id,
        ply=ply,
        # The same three keys `game.move.applied` and `game.snapshot` carry,
        # so a client parses one result shape everywhere.
        result={
            "outcome": outcome,
            "termination_reason": reason,
            "winner": winner if isinstance(winner, str) else None,
        },
        participants=participants,
    )


def _player(seat: object) -> UUID | None:
    if not isinstance(seat, dict):
        return None
    try:
        return UUID(str(seat["player_id"]))
    except (KeyError, ValueError, TypeError):
        return None


__all__ = ["CONSUMER_NAME", "MATCH_COMPLETED", "GatewayCompletionSink"]
