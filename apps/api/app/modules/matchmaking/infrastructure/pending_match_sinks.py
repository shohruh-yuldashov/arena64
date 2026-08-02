"""Where a pending-match offer goes, until there is a socket to put it on.

Implements `application.ports.PendingMatchSink`. A64-015.5 §4 asks for
realtime delivery "reusing the existing gateway/event infrastructure" and
excludes building the live-game WebSocket protocol; AD-09's gateway does not
exist in this build. So the honest terminal adapter records that an offer was
produced, for whom, and about which match — and stops there.

**This is a seam, not a stub**, and the distinction is that everything
upstream of it is real: the offer was made durable in the same transaction
as the match, the relay claimed it, the participant and the deadline were
re-read at delivery, the block graph was re-checked, and the opponent
preview came through `users`' published profile read. What is missing is only
the socket, and the day AD-09's gateway exists it is a second implementation
of this protocol wired at the composition root — nothing above changes.

That is the same argument `LoggingNotificationSink` records, and it has now
held through three tasks.

## What is logged, and what is emphatically not

Counts and identifiers, never the offer:

    recipient_id   already the platform's standard log field
    match_id       the subject, and what an operator traces
    seconds_left   how much of the window survived the pipeline — the one
                   number that says whether realtime delivery is fast
                   enough to be worth having

and never the opponent preview, which is a username and a display name: data
the recipient is entitled to see and that a log aggregator is not entitled to
retain (services.md §8.5).

`INFO`, and **one line per batch** rather than per offer, so a relay tick
carrying twenty matches is one record and not forty (CLAUDE.md §8.8).
"""

import logging
from collections.abc import Sequence

from app.modules.matchmaking.domain.pending_match import PendingMatchOffer

logger = logging.getLogger(__name__)


class LoggingPendingMatchSink:
    """Records deliveries. The only sink until a transport exists.

    Never raises, which is a departure from `PendingMatchSink`'s contract
    that a sink *may* raise — and the departure is the point: a real
    transport failing is something to retry, and this one has nothing to
    fail. Writing a log line that threw would fail an offer delivery for a
    reason that has nothing to do with the delivery, and CLAUDE.md §8.10 is
    explicit that logging never changes behaviour.
    """

    async def deliver(self, offers: Sequence[PendingMatchOffer]) -> None:
        if not offers:
            return

        logger.info(
            "pending_match_offers_delivered",
            extra={
                "offers": len(offers),
                "recipient_ids": [str(offer.recipient_id) for offer in offers],
                "match_ids": sorted({str(offer.match_id) for offer in offers}),
                "previews_withheld": sum(1 for offer in offers if offer.opponent is None),
            },
        )


class NullPendingMatchSink:
    """Delivers nothing.

    For a deployment that wants the consumer wired and silent, and for a
    test whose subject is the resolution rather than the delivery. A real
    class rather than `None`, so the consumer holds a sink unconditionally
    and no call site grows an `if` that would outlive the reason for it.
    """

    async def deliver(self, offers: Sequence[PendingMatchOffer]) -> None:
        return None


__all__ = ["LoggingPendingMatchSink", "NullPendingMatchSink"]
