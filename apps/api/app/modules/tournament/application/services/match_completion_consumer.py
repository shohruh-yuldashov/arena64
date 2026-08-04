"""The consumer that turns `game.match_completed` into an advancement.

`tournament`'s only outbox entry point, and the one that makes A64-019.0's
`origin` / `origin_ref` more than two columns: `game` hands the reference
back, this recognises the match as one of its own, and the bracket moves.

## Why the decoding lives here rather than on the event

`game` publishes a `MatchCompleted` object; the relay stores its `payload()`
and hands this consumer a `dict`. Reconstructing the event type would make
`tournament` import `game`'s domain — which R-1 forbids and the import
contract refuses — so what crosses is the payload, and this is where it
becomes a `CompletedTournamentMatch`.

## Three reasons an entry is skipped, and none of them is a failure

    not a tournament match     `origin` is `queue`; every other consumer's
                               entries pass through here too
    no reference               a tournament match always has one, so this
                               is a payload from before A64-019.5
    no attempt                 nothing here was expecting this match. The
                               reconciler repairs it; retrying cannot

The relay marks everything not named in the return value as delivered, so
"skipped" and "done" are the same to it. That is correct for all three:
none becomes true by being retried.

## Per-entry failures, never a raise

One poison entry must not hold back the batch beside it, and for a bracket
"held back" means a tournament that stops advancing while every part of it
reports healthy.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from app.modules.tournament.application.services.advancement_service import (
    DARK_SEAT,
    LIGHT_SEAT,
    CompletedTournamentMatch,
    TournamentAdvancementService,
    UnknownAttempt,
)
from app.modules.tournament.domain.attempts import AttemptOutcome
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: The one event this consumer wants.
MATCH_COMPLETED: Final = "game.match_completed"

#: The ledger's partition key. Renaming it re-delivers every retained event
#: to the new name, which is a migration rather than a rename.
CONSUMER_NAME: Final = "tournament.match_completed"

#: `MatchOrigin.TOURNAMENT`'s value, as the payload spells it.
#:
#: A literal rather than the imported enum, because what arrives is a
#: serialised string and comparing it to `MatchOrigin.TOURNAMENT.value`
#: would suggest the payload carries an enum. It does not.
TOURNAMENT_ORIGIN: Final = "tournament"

#: `game.domain.result.MatchOutcome`'s two members this module acts on,
#: spelled as the payload carries them. `none` — an aborted match — is
#: deliberately absent: it is not a result, and §6c has no rule that
#: advances anybody on one.
_DECISIVE: Final = "win"
_DRAW: Final = "draw"


@dataclass(frozen=True, slots=True)
class _Failure:
    """One entry this consumer could not process — `outbox.ports.EventFailure`."""

    entry_id: UUID
    reason: str


class TournamentMatchCompletionConsumer:
    """`game.match_completed` -> a bracket advancement."""

    def __init__(self, advancement: TournamentAdvancementService) -> None:
        self._advancement = advancement

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        # Answered from a constant, without I/O: the relay asks per entry.
        return event_type == MATCH_COMPLETED

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failure]:
        """One batch. Returns only what could not be processed.

        Sequential rather than concurrent: two completions in one round
        contend on the same bracket rows, and the batch is small — one entry
        per completed game — so the ordering costs nothing and removes a
        class of deadlock.
        """
        failures: list[_Failure] = []

        for entry in entries:
            completion = _decoded(entry.payload)
            if completion is None:
                continue

            try:
                await self._advancement.apply(completion)
            except UnknownAttempt:
                # Not ours, or not yet recorded. Neither becomes true by
                # being retried — see this module's docstring.
                logger.info(
                    "tournament_completion_unclaimed",
                    extra={"match_id": str(completion.match_id)},
                )
            except Exception as exc:  # noqa: BLE001 — one entry must not stop a batch
                logger.error(
                    "tournament_completion_failed",
                    extra={
                        "entry_id": str(entry.id),
                        "match_id": str(completion.match_id),
                        "error": type(exc).__name__,
                    },
                    exc_info=exc,
                )
                failures.append(_Failure(entry_id=entry.id, reason=type(exc).__name__))

        return failures


def _decoded(payload: dict[str, Any]) -> CompletedTournamentMatch | None:
    """A completion payload as this module's input, or `None` to skip it.

    `None` for anything this bracket has no business acting on: another
    origin, a missing reference, an unusable id, or an outcome that is not a
    result. Never a guess — an advancement is a permanent competitive
    record, and a default here would be one nobody chose.
    """
    if payload.get("origin") != TOURNAMENT_ORIGIN:
        return None

    outcome = _outcome(payload.get("outcome"))
    if outcome is None:
        logger.warning(
            "tournament_completion_outcome_ignored",
            extra={"outcome": str(payload.get("outcome"))},
        )
        return None

    try:
        reference = payload["origin_ref"]
        if reference is None:
            return None
        return CompletedTournamentMatch(
            match_id=UUID(payload["match_id"]),
            pairing_id=UUID(reference),
            outcome=outcome,
            winner_seat=_seat(payload.get("winner")),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("tournament_completion_payload_unusable")
        return None


def _outcome(raw: object) -> AttemptOutcome | None:
    """`game`'s outcome in this module's vocabulary — §6c.

    Translated rather than imported: a tournament cares only whether the
    node was decided, and `MatchOutcome` crossing the boundary would put a
    `game` enum in the bracket's own record.

    An aborted match (`none`) maps to nothing. It is not a draw — nothing
    was played — and §6c's rematch is a rule about games that were.
    """
    if raw == _DECISIVE:
        return AttemptOutcome.DECISIVE
    if raw == _DRAW:
        return AttemptOutcome.DRAW
    return None


def _seat(raw: object) -> str | None:
    """The winning seat, or `None`. Anything unrecognised is a draw's `None`
    rather than a guess at a side."""
    return raw if raw in (LIGHT_SEAT, DARK_SEAT) else None


__all__ = [
    "CONSUMER_NAME",
    "MATCH_COMPLETED",
    "TOURNAMENT_ORIGIN",
    "TournamentMatchCompletionConsumer",
]
