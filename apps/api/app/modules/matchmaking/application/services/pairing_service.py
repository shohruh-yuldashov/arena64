"""`PairingService` — one pool, one scan, at most one match.

Orchestrates; does not decide (services.md §3.2). Who is compatible is
`PairingEngine`'s, which pairs must never be formed is `friends`', the
atomic claim is the repository's, and whether a match may exist is
`game`'s. What is left here is the order of those calls and the
transaction boundary around each — which is the part that has to be right
when a worker dies halfway.

## The flow, and why it is three transactions

    read      snapshot one pool, batch the exclusions        no transaction
    claim     lock both tickets, reserve them                transaction 1
    create    ask `game` for a match                         no transaction
    settle    mark both matched, publish the event           transaction 2
              — or release both back to `waiting`            transaction 2'

**The create step is outside a transaction on purpose.** services.md BE-05
forbids a cross-context call inside an open one: holding two row locks
across another module's work makes the lock-acquisition order something
nobody can reason about, and a slow `game` would become a queue-wide stall.
The price of letting go is the reservation, and `reserved` is what pays it
— see `QueueTicket` on why the state had to exist.

**The claim commits before `game` is called.** That is what makes the
reservation visible to every other worker, which is the entire point: a
second scan reading this pool a millisecond later sees two fewer waiting
tickets and looks elsewhere, rather than selecting the same pair and losing
a race.

## What a crash costs, at each point

    before the claim commits      nothing; the tickets were never touched
    after it, before `game`       two reserved tickets, released by the
                                  next expiry sweep once their window
                                  closes (`claim_due` covers `reserved`)
    after `game`, before settle   a match exists and its tickets say
                                  `reserved`. The retry re-derives the
                                  same `pairing_id`, `game` returns the
                                  same match with `created=False`, and the
                                  settle completes. This is the case
                                  §11's idempotency contract exists for.

## No pool enumeration here

This service is handed **one** `QueuePool`. Which pools to scan, and how
often, is the scheduler's question — see `infrastructure/tasks.py`. A
service that discovered its own work would be a service that could not be
called for a single pool, which is exactly what a test and a future
per-pool worker both need.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.friends.public import PairingExclusions
from app.modules.game.public import (
    CreateMatchRequest,
    MatchCreationRefused,
    MatchCreationUseCase,
    MatchParticipant,
    game_engine_version,
)
from app.modules.matchmaking.application.ports import QueueRepository, RecentOpponentProvider
from app.modules.matchmaking.domain.events import PlayersPaired
from app.modules.matchmaking.domain.pairing import PairExclusions, PairingEngine, TicketPair
from app.modules.matchmaking.domain.queue_pool import QueuePool, QueueType
from app.modules.matchmaking.domain.queue_ticket import QueueTicket
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PairingOutcome:
    """What one `pair_once` did.

    Returned rather than only logged, which is the shape `ExpirySweep`,
    `RelayTick` and `SweepResult` already use: a test asserts on the
    outcome and the worker logs it once.

    The four states are mutually exclusive and are distinguished because an
    operator acts differently on each — an idle pool is normal, a refused
    creation is a `game` problem, and a lost claim means two workers are
    scanning one pool more often than they need to.
    """

    scanned: int
    """How many waiting tickets the scan considered."""

    pairing_id: UUID | None = None
    match_id: UUID | None = None

    claim_lost: bool = False
    """Another worker reserved at least one of the two tickets first. Not a
    failure — the tickets are still waiting and the next tick reconsiders
    them."""

    creation_refused: bool = False
    """`game` declined, and both tickets were released back to `waiting`."""

    @property
    def paired(self) -> bool:
        return self.match_id is not None


class PairingService:
    """The pairing use case. One pool per call.

    Holds ports only — a repository, the pure engine, two exclusion
    providers, `game`'s command port, a publisher, a unit of work and a
    clock — so the whole flow above is testable with no database, no Redis
    and no timer.
    """

    def __init__(
        self,
        *,
        tickets: QueueRepository,
        engine: PairingEngine,
        exclusions: PairingExclusions,
        opponents: RecentOpponentProvider,
        matches: MatchCreationUseCase,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
        candidate_batch_size: int,
    ) -> None:
        self._tickets = tickets
        self._engine = engine
        self._exclusions = exclusions
        self._opponents = opponents
        self._matches = matches
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._candidate_batch_size = candidate_batch_size

    async def pair_once(self, *, pool: QueuePool) -> PairingOutcome:
        """Scans one pool and creates at most one match.

        Never raises. This runs from a scheduled task, and a scan that
        propagated would stop the schedule — the argument
        `QueueService.expire_due`, `OutboxRelay.run_once` and
        `PresenceSweeper.sweep_once` all make. Every failure is recorded
        and returned as an outcome instead.
        """
        now = self._clock.now()
        snapshot = await self._tickets.queue_snapshot(
            pool=pool, now=now, limit=self._candidate_batch_size
        )
        candidates = snapshot.tickets
        if len(candidates) < 2:
            return PairingOutcome(scanned=len(candidates))

        exclusions = await self._exclusions_for(candidates)
        pair = self._engine.select(candidates, now=now, exclusions=exclusions)
        if pair is None:
            logger.debug(
                "pairing_found_nobody",
                extra={"pool": pool.identifier(), "scanned": len(candidates)},
            )
            return PairingOutcome(scanned=len(candidates))

        return await self._settle(pair, pool=pool, scanned=len(candidates))

    async def _exclusions_for(self, candidates: Sequence[QueueTicket]) -> PairExclusions:
        """Every "these two, never" rule for this batch, in two batch reads.

        Two calls for a whole pool rather than two per candidate — the N+1
        CLAUDE.md §10.4 names, and the one that matters most here because
        this runs continuously rather than per request.

        Both are read **before** any lock is taken, for the reason BE-05
        gives: they are cross-context reads, and a cross-context read inside
        an open transaction is a lock-ordering problem waiting for load.
        """
        player_ids = [ticket.player_id for ticket in candidates]
        blocked: Mapping[UUID, frozenset[UUID]] = await self._exclusions.blocked_pairs_among(
            player_ids
        )
        recent: Mapping[UUID, frozenset[UUID]] = await self._opponents.recent_opponents_among(
            player_ids
        )
        return PairExclusions.merged(blocked, recent)

    async def _settle(self, pair: TicketPair, *, pool: QueuePool, scanned: int) -> PairingOutcome:
        """Claim, create, and record — the three steps after a pair is
        chosen. Split out so `pair_once` reads as the decision it is."""
        claimed = await self._claim(pair)
        if claimed is None:
            logger.info(
                "pairing_claim_lost",
                extra={"pool": pool.identifier(), "pairing_id": str(pair.pairing_id)},
            )
            return PairingOutcome(scanned=scanned, pairing_id=pair.pairing_id, claim_lost=True)

        try:
            result = await self._matches.create_match(self._request_for(claimed, pool=pool))
        except MatchCreationRefused as refusal:
            await self._release(claimed, pool=pool, reason=type(refusal).__name__)
            return PairingOutcome(
                scanned=scanned, pairing_id=pair.pairing_id, creation_refused=True
            )
        except Exception as error:  # noqa: BLE001 — a background scan must not escalate
            # Anything that is not a refusal is a fault rather than a
            # decision — an unreachable database, a bug in `game`. The
            # compensation is identical, because the tickets are in the
            # same state either way and two players are waiting on them.
            logger.error(
                "pairing_match_creation_failed",
                extra={
                    "pool": pool.identifier(),
                    "pairing_id": str(claimed.pairing_id),
                    "error": type(error).__name__,
                },
                exc_info=error,
            )
            await self._release(claimed, pool=pool, reason=type(error).__name__)
            return PairingOutcome(
                scanned=scanned, pairing_id=claimed.pairing_id, creation_refused=True
            )

        await self._complete(claimed, pool=pool, match_id=result.match_id)
        logger.info(
            "players_paired",
            extra={
                "pool": pool.identifier(),
                "pairing_id": str(claimed.pairing_id),
                "match_id": str(result.match_id),
                # `match_created`, not `created`: `created` is a reserved
                # `LogRecord` attribute and `Logger.makeRecord` raises
                # `KeyError` on a collision — which would turn a successful
                # pairing into an exception at its very last step, after
                # the match existed and both tickets were settled.
                # CLAUDE.md §8.10: logging never changes behaviour.
                "match_created": result.created,
                "scanned": scanned,
            },
        )
        return PairingOutcome(
            scanned=scanned, pairing_id=claimed.pairing_id, match_id=result.match_id
        )

    async def _claim(self, pair: TicketPair) -> TicketPair | None:
        """Locks both tickets and reserves them, in one transaction.

        Returns the pair **as reserved** — rebuilt from the rows the claim
        actually locked rather than from the snapshot, because those are
        the values whose `status` the compare-and-set below will match
        against. Returns `None` when either ticket was gone, which is the
        ordinary outcome of two workers scanning one pool.

        The sides survive the rebuild: `TicketPair.of` derives them from
        the two ticket ids, which the claim cannot change.
        """
        now = self._clock.now()
        async with self._unit_of_work:
            claimed = await self._tickets.claim_pair(list(pair.ticket_ids()), now=now)
            if len(claimed) != 2:
                await self._unit_of_work.rollback()
                return None

            reserved = [ticket.reserved() for ticket in claimed]
            if not await self._tickets.reserve(reserved):
                # Unreachable while the claim holds the row locks, and
                # checked anyway: the day this method is split from the
                # lock, a silent half-reservation would strand a player.
                await self._unit_of_work.rollback()
                return None

            await self._unit_of_work.commit()

        return TicketPair.of(reserved[0], reserved[1])

    def _request_for(self, pair: TicketPair, *, pool: QueuePool) -> CreateMatchRequest:
        """The command `game` receives.

        The engine version is stamped here, at the moment of pairing, from
        `game.public.game_engine_version()` — AD-15, and read through the
        published surface so `matchmaking` never imports the engine (R-2).
        """
        return CreateMatchRequest(
            pairing_id=pair.pairing_id,
            variant=pool.variant,
            rated=pool.queue_type is QueueType.RANKED,
            engine_version=game_engine_version(),
            light=MatchParticipant(player_id=pair.light.player_id, queue_ticket_id=pair.light.id),
            dark=MatchParticipant(player_id=pair.dark.player_id, queue_ticket_id=pair.dark.id),
        )

    async def _complete(self, pair: TicketPair, *, pool: QueuePool, match_id: UUID) -> None:
        """Marks both tickets matched and publishes the pairing, together.

        One transaction, so the event is exactly as durable as the
        transitions it announces (AD-16). A consumer cannot learn about a
        match whose tickets rolled back.
        """
        at = self._clock.now()
        matched = [pair.light.matched(at), pair.dark.matched(at)]

        async with self._unit_of_work:
            if not await self._tickets.complete(matched, at=at):
                await self._unit_of_work.rollback()
                # The one genuinely bad outcome: `game` has the match and
                # the tickets do not say so. Reachable only if a reserved
                # ticket was resolved underneath us — today, by an expiry
                # sweep taking a reservation whose window closed
                # mid-pairing. `ERROR` with both identifiers, because the
                # reconciliation is manual until a match carries a durable
                # link back to its tickets (A64-015.4).
                logger.error(
                    "pairing_settle_failed",
                    extra={
                        "pool": pool.identifier(),
                        "pairing_id": str(pair.pairing_id),
                        "match_id": str(match_id),
                        "ticket_ids": [str(ticket_id) for ticket_id in pair.ticket_ids()],
                    },
                )
                return

            await self._events.publish(
                PlayersPaired(
                    occurred_at=at,
                    match_id=match_id,
                    pairing_id=pair.pairing_id,
                    variant=pool.variant,
                    queue_type=pool.queue_type,
                    region=pool.region,
                    light_player_id=pair.light.player_id,
                    dark_player_id=pair.dark.player_id,
                    light_ticket_id=pair.light.id,
                    dark_ticket_id=pair.dark.id,
                    waited_for_seconds=_longest_wait(pair, at),
                )
            )
            await self._unit_of_work.commit()

    async def _release(self, pair: TicketPair, *, pool: QueuePool, reason: str) -> None:
        """Returns both reserved tickets to `waiting` — §10's compensation.

        **No event.** Nothing durable happened: two tickets were reserved
        for a moment and are waiting again, with the `entered_at` they
        always had. Publishing "a pairing was attempted and abandoned"
        would announce an implementation detail of a background job to
        every subscriber.

        `WARNING` rather than `ERROR` for the ordinary path, because a
        refusal is a decision `game` made and the queue recovered from it
        exactly as designed. A release that itself fails is the `ERROR`.
        """
        async with self._unit_of_work:
            released = [pair.light.released(), pair.dark.released()]
            if not await self._tickets.release(released):
                await self._unit_of_work.rollback()
                logger.error(
                    "pairing_release_failed",
                    extra={
                        "pool": pool.identifier(),
                        "pairing_id": str(pair.pairing_id),
                        "ticket_ids": [str(ticket_id) for ticket_id in pair.ticket_ids()],
                        "reason": reason,
                    },
                )
                return
            await self._unit_of_work.commit()

        logger.warning(
            "pairing_compensated",
            extra={
                "pool": pool.identifier(),
                "pairing_id": str(pair.pairing_id),
                "reason": reason,
            },
        )


def _longest_wait(pair: TicketPair, until: datetime) -> float:
    """How long the longer-waiting of the two had been in the pool.

    See `PlayersPaired.waited_for_seconds` on why one number rather than
    two: the question is about the pool, and the match could not have
    happened before the older ticket was entered.
    """
    entered = min(pair.light.entered_at, pair.dark.entered_at)
    return (until - entered).total_seconds()
