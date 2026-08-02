"""`PersistentMatchCreation` — `game`'s real answer to a pairing.

Implements `game.public.MatchCreationUseCase`, replacing A64-015.3's
`UnavailableMatchCreation`. Orchestrates; does not decide (services.md
§3.2): what a valid match looks like is `MatchRecord`'s, uniqueness is
`uq_match__pairing_id`'s, and what is left here is one transaction and the
event that rides in it.

## The whole of idempotency is one insert and one re-read

A64-015.4 §3 forbids two things by name — in-memory deduplication, and
check-then-insert without a constraint — and both are forbidden for the
same reason: two pairing workers retrying one pairing pass any check
simultaneously. So this inserts unconditionally, lets the unique index
refuse the loser, and re-reads by `pairing_id`. The race is resolved by
PostgreSQL, deterministically, and both callers come away holding the same
`match_id`.

The `created` flag on the result is what tells them apart, and no caller
branches on it: both outcomes mean "the match exists, settle the tickets".
It exists so a **metric** can show a retry storm, and so a test can prove
the second call did not create a second match.

## Why the event is published only on the first call

A retry that found an existing match publishes nothing. `MatchCreated`
announces a fact, and the fact happened once; a second row would tell a
notification consumer to offer the same match twice, and the outbox's
at-least-once delivery already means every consumer must tolerate one
duplicate without being handed extras by the producer.
"""

import logging

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.game.application.ports import MatchRecordRepository
from app.modules.game.domain.events import MatchCreated
from app.modules.game.domain.match_record import MatchRecord, MatchSeat
from app.modules.game.public.matches import CreateMatchRequest, CreateMatchResult
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class PersistentMatchCreation:
    """The match-creation use case, over one session.

    Holds ports only — a repository, a publisher, a unit of work and a
    clock — so the whole flow is testable with no database and no timer.
    """

    def __init__(
        self,
        *,
        matches: MatchRecordRepository,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._matches = matches
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def create_match(self, request: CreateMatchRequest) -> CreateMatchResult:
        """Creates the match this pairing produced, or returns the one that
        already exists for it.

        One transaction, so the `match.created` outbox row is exactly as
        durable as the match (AD-16). A caller that saw a `match_id` and a
        consumer that saw the event cannot disagree about whether the match
        exists.
        """
        at = self._clock.now()
        record = MatchRecord(
            pairing_id=request.pairing_id,
            variant=request.variant,
            rated=request.rated,
            engine_version=request.engine_version,
            light=MatchSeat(
                player_id=request.light.player_id,
                queue_ticket_id=request.light.queue_ticket_id,
            ),
            dark=MatchSeat(
                player_id=request.dark.player_id,
                queue_ticket_id=request.dark.queue_ticket_id,
            ),
            created_at=at,
            acceptance_deadline=request.acceptance_deadline,
        )

        async with self._unit_of_work:
            stored, created = await self._matches.create(record)
            if created:
                await self._events.publish(
                    MatchCreated(
                        occurred_at=stored.created_at,
                        match_id=stored.id,
                        pairing_id=stored.pairing_id,
                        light_player_id=stored.light.player_id,
                        dark_player_id=stored.dark.player_id,
                        variant=stored.variant,
                        rated=stored.rated,
                        acceptance_deadline=stored.acceptance_deadline,
                    )
                )
            await self._unit_of_work.commit()

        logger.info(
            "match_created",
            extra={
                "match_id": str(stored.id),
                "pairing_id": str(stored.pairing_id),
                # `match_created`, not `created`: `created` is a reserved
                # `LogRecord` attribute and `Logger.makeRecord` raises
                # `KeyError` on a collision, which would turn a successful
                # creation into an exception at its very last step.
                # CLAUDE.md §8.10: logging never changes behaviour.
                "match_is_new": created,
                "variant": stored.variant.value,
                "rated": stored.rated,
            },
        )
        return CreateMatchResult(match_id=stored.id, pairing_id=stored.pairing_id, created=created)


__all__ = ["PersistentMatchCreation"]
