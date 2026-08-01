"""The implementation of `application.ports.RatingSnapshotProvider`.

One class, and it returns a constant. That is the honest implementation of
"what is this player's rating" on a platform where domain-model.md Q-3 —
"Elo or Glicko-2, or another system?" — is an **open question** and the
`rating` module does not exist.

## Why this is not a stub, and what would make it one

The distinction `PresenceRecorder` draws in `users.public.ports`: a stub is
a method with no caller and no correctness story, while this has both.

QT-2's rule is that a ticket carries the rating it was entered with rather
than a reference to a live one, and that rule is *fully implemented* — the
column exists, the aggregate holds it, the event carries it, and nothing
re-reads it while the ticket waits. What is provisional is only the number,
and PR-6 already says how a provisional rating must be treated: marked, and
never mistaken for a measurement.

The seam is what matters. On the day `rating` ships, its published reader
satisfies this port, `matchmaking.presentation.dependencies` names it
instead of this class, and no use case, no aggregate and no test changes.
Reaching for `PROVISIONAL_RATING` inside `QueueService` would have been one
line shorter and would have made that day a change to a service.

## Why it does not read `profiles`

`profiles` composes a public profile and reports `PlayerRatings.unrated()`
— the same 1500, for the same reason. It is not published (`profiles.public`
exposes one port, a profile renderer), and importing another module's
`domain` is exactly what R-1 forbids.

Nor should it be published. A *public profile's* starting value and a
*matchmaker's* are the same number by coincidence: the first is what a
stranger sees and is governed by privacy, the second is a pairing input that
must be deterministic within a scan. Coupling them would mean a change to
what profiles display could move who gets paired with whom.
"""

import logging
from uuid import UUID

from app.modules.matchmaking.domain.queue_ticket import PROVISIONAL_RATING, QueueType

logger = logging.getLogger(__name__)


class ProvisionalRatingProvider:
    """Every player rates at the provisional starting value.

    Stateless and infallible: no session, no Redis, no settings. It ignores
    both arguments, which is the honest signature when the answer is the
    same for everyone — and the reason there is no `NoRatingProvider`
    fallback beside it, because there is nothing here that can fail.

    Logged at `DEBUG` rather than `WARNING`, unlike the platform's other
    provisional adapters. `NoPresenceProvider` and
    `NoMatchesStatisticsProvider` are *degradations* — a working feature
    switched off — and an operator needs to know one is running. This is
    not a degradation: it is the only answer that exists, on every tier,
    until `rating` is built.
    """

    async def rating_for(self, player_id: UUID, *, queue_type: QueueType) -> int:
        logger.debug(
            "rating_snapshot_provisional",
            extra={"player_id": str(player_id), "queue_type": queue_type.value},
        )
        return PROVISIONAL_RATING
