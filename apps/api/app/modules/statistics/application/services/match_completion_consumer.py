"""The consumer that turns `game.match_completed` into player statistics.
A64-020.5F §8.

`statistics` shipped as the reading half of a projection because "the
writing half is a consumer of `match.completed` and there is no `game`
module to emit one". There is now, and this is that consumer.

## What it reads, and what it deliberately does not

Everything from the **payload**: the match id, both seats' player ids, the
outcome and the winner. No live match read, no profile read, no rating
read — §8 forbids them and the event already carries the facts, so a query
here would be a second answer to a question the event settled.

The completion instant comes from the **envelope** (`OutboxEntry.occurred_at`)
rather than the payload, because `MatchCompleted` does not carry one. That
is the instant the event was staged, inside the same transaction that
settled the match, so it *is* the completion time to within the transaction
— and it is the same value the backfill reads as `ended_at`.

## Sequential, not concurrent

The same choice `rating`'s consumer makes and for the same reason: each
entry is its own transaction over two player rows, and two completions
sharing a player would contend on the same rows. A batch is one entry per
completed game, so the ordering costs nothing and removes a class of
deadlock.

## Failures

An entry that cannot be read is skipped and **not retried** — a payload
missing a seat will still be missing one on the next attempt, and retrying
it forever would keep an unprocessable event at the head of the backlog. An
entry that *failed* — the database was unavailable, the transaction lost a
race — is reported, and the relay retries it.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.modules.statistics.application.ports import MatchProjectionUseCase
from app.modules.statistics.application.services.match_projection_service import (
    CompletedMatchFacts,
    ProjectionOutcome,
)
from app.modules.statistics.metrics import (
    STATISTICS_PROJECTIONS,
    ProjectionResult,
)
from app.platform.metrics import MetricsRecorder
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: The one event this consumer wants.
MATCH_COMPLETED: Final = "game.match_completed"

#: The consumer's name in `processed_event`. Namespaced, matching
#: `rating.match_completed`, so two modules consuming one event keep
#: separate ledgers.
CONSUMER_NAME: Final = "statistics.match_completed"


@dataclass(frozen=True, slots=True)
class _Failure:
    """One entry this consumer could not process — `outbox.ports.EventFailure`."""

    entry_id: UUID
    reason: str


class StatisticsMatchCompletionConsumer:
    """Folds `game.match_completed` into `player_statistics`."""

    def __init__(self, *, projections: MatchProjectionUseCase, metrics: MetricsRecorder) -> None:
        self._projections = projections
        self._metrics = metrics

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        # Answered from a constant, without I/O: the relay asks per entry.
        return event_type == MATCH_COMPLETED

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failure]:
        """One batch. Returns only what could not be processed."""
        failures: list[_Failure] = []

        for entry in entries:
            facts = _decoded(entry.payload, completed_at=entry.occurred_at)
            if facts is None:
                # Unprocessable rather than failed — see the module
                # docstring on why this is not retried.
                self._metrics.increment(
                    STATISTICS_PROJECTIONS, labels={"result": ProjectionResult.REJECTED}
                )
                logger.warning(
                    "statistics_completion_unreadable", extra={"entry_id": str(entry.id)}
                )
                continue

            try:
                outcome = await self._projections.apply(facts)
            except Exception as exc:  # noqa: BLE001 — one entry must not stop a batch
                self._metrics.increment(
                    STATISTICS_PROJECTIONS, labels={"result": ProjectionResult.FAILED}
                )
                logger.error(
                    "statistics_consumer_failed",
                    extra={"entry_id": str(entry.id), "error": type(exc).__name__},
                    exc_info=exc,
                )
                failures.append(_Failure(entry_id=entry.id, reason=type(exc).__name__))
                continue

            self._metrics.increment(STATISTICS_PROJECTIONS, labels={"result": _RESULTS[outcome]})

        return failures


#: One outcome as one bounded metric label. A mapping rather than a cast,
#: so a new outcome is a `KeyError` here rather than a new time series
#: nobody decided on.
_RESULTS: Final[dict[ProjectionOutcome, ProjectionResult]] = {
    ProjectionOutcome.APPLIED: ProjectionResult.APPLIED,
    ProjectionOutcome.ALREADY_PROCESSED: ProjectionResult.ALREADY_PROCESSED,
    ProjectionOutcome.IGNORED_NON_COUNTING: ProjectionResult.IGNORED,
    ProjectionOutcome.REJECTED_INVALID: ProjectionResult.REJECTED,
}


def _decoded(payload: dict[str, Any], *, completed_at: datetime) -> CompletedMatchFacts | None:
    """A completion payload as this module's input, or `None`.

    `None` for anything that cannot be counted from the payload alone — a
    match with no seat summaries, a malformed identifier, a missing
    outcome. **Never a guess**: a fabricated seat would put a game on
    somebody's permanent record that they did not play.

    `light` and `dark` are typed `SeatSummary | None` on the event, and the
    `None` case is real: a match created before ratings were captured
    carries no seat at all. Such a match cannot be attributed and is
    skipped, which is the same answer `rating`'s consumer gives it.
    """
    light = _player(payload.get("light"))
    dark = _player(payload.get("dark"))
    if light is None or dark is None or light == dark:
        return None

    outcome = payload.get("outcome")
    if not isinstance(outcome, str):
        return None

    winner = payload.get("winner")
    try:
        match_id = UUID(str(payload["match_id"]))
    except (KeyError, ValueError, TypeError):
        return None

    return CompletedMatchFacts(
        match_id=match_id,
        light_player_id=light,
        dark_player_id=dark,
        outcome=outcome,
        winner=winner if isinstance(winner, str) else None,
        completed_at=completed_at,
    )


def _player(seat: Any) -> UUID | None:
    if not isinstance(seat, dict):
        return None
    try:
        return UUID(str(seat["player_id"]))
    except (KeyError, ValueError, TypeError):
        return None


__all__ = [
    "CONSUMER_NAME",
    "MATCH_COMPLETED",
    "StatisticsMatchCompletionConsumer",
]
