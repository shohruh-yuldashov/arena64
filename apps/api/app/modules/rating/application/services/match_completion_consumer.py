"""The consumer that turns `game.match_completed` into a rating change.

A64-017.6, closing the seam A64-017.3 left open: `MatchRatingService` was
built, tested and **never called**. The whole module was unreachable — a
match completed, the outbox row was written, and nothing consumed it, so no
rating on this platform ever moved.

That is the same defect A64-016.8 found in the cross-node bus, and it fails
the same way: every part works, the metrics of every part look healthy, and
the feature does not exist.

## Why the decoding lives here rather than on the event

`game` publishes a `MatchCompleted` object; the relay stores its `payload()`
and hands this consumer a `dict`. Reconstructing the event type would make
`rating` import `game`'s domain — which R-1 forbids and the import contract
refuses — so what crosses is the payload, and this is where it becomes a
`CompletedMatch`.

**A payload that cannot be decoded is not a failure.** A match created
before A64-017.2 carries no seat snapshots, and there is nothing to compute
from: the correct answer is "not rateable", the same as a casual game.
Returning a failure would make the relay retry it forever.

## Per-entry failures, never a raise

The relay marks everything not named in the return value as delivered, so a
handler that swallowed an error would be asserting success. One poison entry
must not hold back the batch beside it — and for a rating, "held back" means
a ladder that stops moving.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from app.modules.game.public import ProductVariant, TerminationReason
from app.modules.rating.application.services.match_rating_service import (
    CompletedMatch,
    CompletedSeat,
    MatchRatingOutcome,
    MatchRatingService,
)
from app.modules.rating.domain.keys import RatingKey, SpeedClass
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: The one event this consumer wants.
MATCH_COMPLETED: Final = "game.match_completed"

#: The ledger's partition key. Renaming it re-delivers every retained event
#: to the new name, which is a migration rather than a rename.
CONSUMER_NAME: Final = "rating.match_completed"


@dataclass(frozen=True, slots=True)
class _Failure:
    """One entry this consumer could not process — `outbox.ports.EventFailure`."""

    entry_id: UUID
    reason: str


class MatchCompletionConsumer:
    """`game.match_completed` -> a rating update. The module's only entry point."""

    def __init__(self, ratings: MatchRatingService) -> None:
        self._ratings = ratings

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        # Answered from a constant, without I/O: the relay asks per entry.
        return event_type == MATCH_COMPLETED

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failure]:
        """One batch. Returns only what could not be processed.

        Sequential rather than concurrent, deliberately: each entry is its
        own transaction over two rating rows, and two completions sharing a
        player would contend on the same aggregate. The batch is small — one
        entry per completed game — so the ordering costs nothing and removes
        a class of deadlock.
        """
        failures: list[_Failure] = []

        for entry in entries:
            completion = _decoded(entry.payload)
            if completion is None:
                # Not rateable and not retryable — see the module docstring.
                continue

            try:
                outcome = await self._ratings.apply(completion)
            except Exception as exc:  # noqa: BLE001 — one entry must not stop a batch
                logger.error(
                    "rating_consumer_failed",
                    extra={"entry_id": str(entry.id), "error": type(exc).__name__},
                    exc_info=exc,
                )
                failures.append(_Failure(entry_id=entry.id, reason=type(exc).__name__))
                continue

            if outcome == MatchRatingOutcome.FROZEN:
                # **Not a failure, and not retried.** SPEC-RATING §13: the
                # adjustment is lost rather than queued until `fairplay`
                # exists. Retrying would spin until the hold is lifted, and
                # the hold is lifted by a module that does not exist.
                logger.warning("rating_skipped_frozen", extra={"entry_id": str(entry.id)})

        return failures


def _decoded(payload: dict[str, Any]) -> CompletedMatch | None:
    """A completion payload as this module's input, or `None`.

    `None` for anything that cannot be rated from the payload alone — a
    match with no seat snapshots, an unknown speed class, a malformed id.
    Never a guess: SPEC-RATING §7.6 makes the snapshot the *only* legitimate
    input, so a default here would be a made-up number on a permanent
    record.
    """
    light, dark = _seat(payload.get("light")), _seat(payload.get("dark"))
    if light is None or dark is None:
        return None

    try:
        key = RatingKey(
            variant=ProductVariant(payload["variant"]),
            speed_class=SpeedClass(payload["speed_class"]),
        )
        return CompletedMatch(
            match_id=UUID(payload["match_id"]),
            key=key,
            rated=bool(payload["rated"]),
            termination=TerminationReason(payload["termination_reason"]),
            winner=payload.get("winner"),
            light=light,
            dark=dark,
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("rating_completion_payload_unusable")
        return None


def _seat(raw: object) -> CompletedSeat | None:
    if not isinstance(raw, dict):
        return None
    try:
        return CompletedSeat(
            player_id=UUID(raw["player_id"]),
            value=float(raw["rating_value"]),
            deviation=float(raw["rating_deviation"]),
            volatility=float(raw["rating_volatility"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


__all__ = ["CONSUMER_NAME", "MATCH_COMPLETED", "MatchCompletionConsumer"]
