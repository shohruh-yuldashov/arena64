"""`GatewayPendingMatchSink` — putting a match offer on a socket.
A64-020.5D §2, §3, §4, §10.

The transport A64-015.5 left a seam for. `LoggingPendingMatchSink` recorded
that an offer had been produced and stopped there, because AD-09's gateway
did not exist; it does now, and this is the second implementation of the
same protocol wired at the composition root. **Nothing upstream changes** —
the offer is still made durable in the same transaction as the match, still
claimed by the relay, still re-read at delivery, still privacy-gated.

## Why this lives in `app/gateway/` and not in `matchmaking`

It needs two things: the shape of an offer, and the fleet-wide fan-out.
Putting it in `matchmaking.infrastructure` would make that module import
gateway internals — a module learning that a socket exists. Putting it here
makes the gateway import `matchmaking.public`, which is the same direction
it already imports `game.public` and `friends.public`.

`PendingMatchSink` is a structural `Protocol`, so this class does not import
it: it satisfies the shape, and the composition root is where the two meet.
That is AD-06 working as intended — the port stays in the layer that needs
it, and the adapter stays where the capability is.

## Delivery is an optimisation — §3

Every failure mode here is *tolerable by construction*, because the durable
answer is `GET /matchmaking/matches/pending`:

    nobody connected     counted, not raised. The ordinary state of a
                         player who queued and closed the tab
    a socket dropped     the frame is lost and the read recovers it
    another node         forwarded through the existing bus; the
                         forwarder there delivers it
    the publish raised   counted, not raised — see `deliver`

So this **never raises**, which is a deliberate departure from
`PendingMatchSink`'s "a sink may raise". That contract exists so a real
transport failure is retried; here a retry would re-deliver an offer the
client can already read, and failing the relay tick would hold up every
*other* offer in the same batch. The one thing that must not happen is a
match pairing being undone because a socket was busy.

## Ordering and duplication

Neither is guaranteed and neither needs to be. §3 requires a duplicate to be
idempotent and a late frame to be harmless, and both hold on the client
side: the push is a wake-up signal, and what it wakes the client up to do is
read the truth.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from app.gateway.delivery import RoomBroadcaster
from app.gateway.metrics import MATCH_OFFER_PUSHES, MatchOfferOutcome
from app.gateway.protocol import match_offered
from app.modules.matchmaking.public import PendingMatchOffer
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)


class GatewayPendingMatchSink:
    """Delivers pending-match offers over the fleet's sockets."""

    def __init__(self, *, broadcaster: RoomBroadcaster, metrics: MetricsRecorder) -> None:
        self._broadcaster = broadcaster
        self._metrics = metrics

    async def deliver(self, offers: Sequence[PendingMatchOffer]) -> None:
        """Delivers a batch. An empty batch is a legal no-op.

        **One fan-out per offer**, not per batch, because each offer is
        addressed to a different player and carries a different payload —
        the recipient's own side, and their view of the opponent. A batched
        fan-out would need one frame per recipient anyway.

        Never raises; see this module's docstring.
        """
        for offer in offers:
            await self._push(offer)

    async def _push(self, offer: PendingMatchOffer) -> None:
        try:
            report = await self._broadcaster.deliver(
                match_offered(_payload(offer)),
                # The **recipient only**. A fan-out to both participants
                # would send each player the other's view of the match —
                # their side, their opponent preview, their acceptance
                # flag. §21: an offer reaches exactly the player it names.
                recipients=[offer.recipient_id],
            )
        except Exception as exc:  # noqa: BLE001 — a push must not fail a relay tick
            self._metrics.increment(
                MATCH_OFFER_PUSHES, labels={"outcome": MatchOfferOutcome.FAILED}
            )
            logger.error(
                "match_offer_push_failed",
                extra={"match_id": str(offer.match_id), "error": type(exc).__name__},
                exc_info=exc,
            )
            return

        if report.local > 0:
            outcome = MatchOfferOutcome.LOCAL
        elif report.remote_nodes > 0:
            outcome = MatchOfferOutcome.REMOTE
        else:
            outcome = MatchOfferOutcome.NO_CONNECTION

        self._metrics.increment(MATCH_OFFER_PUSHES, labels={"outcome": outcome})

        # One line per offer, and it carries what an operator traces: which
        # match, and whether anybody was there. **Never the payload** — the
        # opponent preview is a username and a display name, data the
        # recipient may see and a log aggregator may not retain
        # (services.md §8.5, and `LoggingPendingMatchSink` made the same
        # point).
        logger.info(
            "match_offer_pushed",
            extra={
                "match_id": str(offer.match_id),
                "outcome": outcome.value,
                "local": report.local,
                "remote_nodes": report.remote_nodes,
            },
        )


def _payload(offer: PendingMatchOffer) -> dict[str, Any]:
    """One offer as wire primitives — §2.

    Deliberately the **same field names** `PendingMatchResponse` uses on the
    HTTP surface, so a client parses one shape whether it was pushed or
    polled. That is what makes §6's reconciliation a comparison rather than
    a translation.

    What is absent is the point: no queue ticket, no pairing id, no Redis
    key, no ORM row, no rating, no email. Every field below is something the
    recipient could already read from
    `GET /matchmaking/matches/pending`.
    """
    return {
        "match_id": str(offer.match_id),
        "status": offer.status.value,
        "your_side": offer.your_side.value,
        "opponent": _opponent(offer),
        "variant": offer.variant.value,
        "rated": offer.rated,
        "time_control": (
            {
                "initial_ms": offer.time_control.initial_ms,
                "increment_ms": offer.time_control.increment_ms,
            }
            if offer.time_control is not None
            else None
        ),
        "speed_class": offer.speed_class,
        "acceptance_deadline": offer.acceptance_deadline.isoformat(),
        "you_accepted": offer.you_accepted,
        "opponent_accepted": offer.opponent_accepted,
        "created_at": offer.created_at.isoformat(),
    }


def _opponent(offer: PendingMatchOffer) -> Mapping[str, Any] | None:
    """The opponent, or `None` when this recipient may not see them.

    `None` is a **product outcome**, not a failure: a block stands between
    the two, and the offer is still delivered because refusing to pair
    people who blocked each other is `friends`' decision and it has already
    been made upstream.
    """
    if offer.opponent is None:
        return None
    return {
        "player_id": str(offer.opponent.player_id),
        "username": offer.opponent.username,
        "display_name": offer.opponent.display_name,
    }


__all__ = ["GatewayPendingMatchSink"]
