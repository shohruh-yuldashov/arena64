"""`MatchOutcomeService` — what a failed handshake costs each of its two
players. A64-015.5 §1.

A64-015.4 shipped acceptance with the failure policy explicitly **open**:
"when a player declines or lets the window close, both queue tickets stay
`matched` and neither player is re-queued. That is the safest minimal
behaviour, and it is chosen rather than found." It also named the cost: "a
player who accepts promptly and whose opponent declines loses their place in
line through no fault of their own."

This service is the answer, and it is an `EventHandler` on the outbox rather
than a branch inside acceptance — see below.

## The policy, in one table

The rule is uniform and has one exception. **A participant who accepted is
requeued; a participant who explicitly declined earns a cooldown.** Silence
earns neither.

| What happened | The accepting player | The other player |
| --- | --- | --- |
| One accepted, one declined | requeued, original `entered_at` | cooldown, not requeued |
| One accepted, one stayed silent | requeued, original `entered_at` | nothing |
| Neither answered | — (nobody accepted) | nothing, for both |

Three properties are worth naming because each was a decision:

**The accepting player keeps their priority, not just their place.**
`QueueTicket.requeued` preserves `entered_at`, which is the pairing order's
sort key *and* the input to QT-5's widening rating window. A fresh instant
would have cost them both, and the second is the one that hurts — they would
be re-entered with a narrow search after already waiting.

**Silence is not a decline.** §3 says so, and the asymmetry is the safe
direction: a decline is an observed decision, while silence has a dozen
causes the platform cannot tell apart (a dead battery, a tunnel, a crashed
tab, somebody who walked away). Punishing all of them for the one that
deserves it would make the queue hostile to anybody on a train.

**Neither player is told what happened to the other.** The requeued player
gets a ticket; they are not told whether their opponent refused them or
simply vanished. That distinction is a fact about somebody else's behaviour,
and the same reasoning that keeps `MatchNotPending` from naming its cause
keeps it out of here.

## Why a consumer and not a branch inside `MatchAcceptanceService`

Three reasons, in order:

1. **It crosses a module.** The decline happens in `game`, and the requeue
   is a `matchmaking` write. Doing both in one transaction would be `game`
   calling into `matchmaking` — an edge architecture.md §7 does not draw,
   and the reverse of the one it does.
2. **The expiry path has no request.** A silent expiry is discovered by the
   reconciler, not by a player, so half the policy would have to live in a
   background job anyway. One implementation for both halves is the only way
   they stay the same policy.
3. **A failed requeue must not fail a decline.** A player pressing "decline"
   must get their `200` even if the queue is briefly unwritable. The outbox
   makes the requeue a durable obligation that retries, rather than a second
   write that can take the first one down with it.

## Idempotency

Three layers, and each covers what the others cannot:

    the ledger    `platform.processed_event` stops a redelivered entry
                  reaching this consumer twice at all
    QT-1          a player who already holds a live ticket is not requeued
    the index     `uq_queue_ticket__requeued_from` refuses a second ticket
                  from one source, so two *concurrent* deliveries produce
                  one row

The cooldown needs none of them: `CooldownRepository.apply` is an upsert
that takes the later expiry, so applying the same cooldown twice is applying
it once. Its **audit row** (A64-015.6 §3) is idempotent on
`(player_id, source_match_id)` for the same reason, and rides in the same
transaction as the bar it explains.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.game.public import MatchAcceptanceExpired, MatchDeclined
from app.modules.matchmaking.application.metrics import (
    ACCEPTANCE_FAILURE_ACTIONS,
    RECONCILIATION_ACTIONS,
    AcceptanceFailureAction,
)
from app.modules.matchmaking.application.ports import (
    CooldownAuditRepository,
    CooldownRepository,
)
from app.modules.matchmaking.application.services.queue_service import QueueService
from app.modules.matchmaking.domain.cooldown import QueueCooldown
from app.modules.matchmaking.domain.cooldown_audit import CooldownRecord
from app.modules.matchmaking.domain.events import ReconciliationAction
from app.platform.metrics import MetricsRecorder
from app.platform.outbox import OutboxEntry

logger = logging.getLogger(__name__)

#: This consumer's name in `platform.processed_event`.
#:
#: A constant rather than a literal at the two sites that use it, because
#: renaming it re-delivers every retained event to the new name — which for
#: *this* consumer means requeueing players whose matches failed weeks ago.
#: See `EventHandler.consumer`.
CONSUMER_NAME = "matchmaking_acceptance_failure"

#: The two events this consumer subscribes to.
#:
#: Built from the classes rather than from strings, so a renamed event fails
#: to import instead of silently never matching — and `matchmaking` may name
#: `game`'s event classes because they are re-exported through `game.public`.
SUBSCRIBED_EVENT_TYPES: frozenset[str] = frozenset(
    {MatchDeclined.event_type, MatchAcceptanceExpired.event_type}
)


@dataclass(frozen=True, slots=True)
class _Failed:
    """One entry this consumer could not process — `outbox.ports.EventFailure`."""

    entry_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class FailedHandshake:
    """One match that ended without a game, as this policy reads it.

    Parsed out of an event payload rather than passed around as a dict, so
    the four ids and the two flags cannot be transposed — the same argument
    `MatchParticipant` makes for carrying a player and their ticket together.
    """

    match_id: UUID
    accepted_ticket_ids: tuple[UUID, ...]
    """The tickets of the players who said yes. Empty when nobody did,
    which is the "neither answered" row of the table above."""

    declined_by_player_id: UUID | None
    """Who explicitly refused, or `None` for a silent expiry. This is the
    field the whole decline-versus-silence distinction turns on."""


class MatchOutcomeService:
    """The acceptance-failure policy, as an outbox consumer.

    Holds ports only — the queue use cases, the cooldown store, a unit of
    work, a clock and a metrics recorder — so the whole policy is testable
    with no database, no `game` and no relay.
    """

    def __init__(
        self,
        *,
        queue: QueueService,
        cooldowns: CooldownRepository,
        audit: CooldownAuditRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
        metrics: MetricsRecorder,
        decline_cooldown_seconds: float,
    ) -> None:
        self._queue = queue
        self._cooldowns = cooldowns
        self._audit = audit
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._metrics = metrics
        self._decline_cooldown_seconds = decline_cooldown_seconds

    @property
    def consumer(self) -> str:
        return CONSUMER_NAME

    def handles(self, event_type: str) -> bool:
        return event_type in SUBSCRIBED_EVENT_TYPES

    async def handle(self, entries: Sequence[OutboxEntry]) -> Sequence[_Failed]:
        """Applies the policy to a batch. Returns the entries that failed.

        Per entry rather than per batch, and that is deliberate against the
        grain of `SocialNotificationDispatcher`, which batches its reads: a
        requeue is a *write* whose outcome depends on the state left by the
        previous one, so two entries for the same player have to be applied
        in order rather than resolved together.

        One entry failing does not fail the batch — the relay records it,
        backs off, and redelivers that entry alone.
        """
        failures: list[_Failed] = []

        for entry in entries:
            try:
                await self._apply(_parse(entry))
            except Exception as error:  # noqa: BLE001 — one event must not fail the batch
                logger.warning(
                    "acceptance_failure_policy_failed",
                    extra={
                        "event_id": str(entry.id),
                        "event_type": entry.event_type,
                        "error": type(error).__name__,
                    },
                    exc_info=error,
                )
                failures.append(_Failed(entry_id=entry.id, reason=type(error).__name__))

        return failures

    async def _apply(self, handshake: FailedHandshake) -> None:
        """One failed handshake: requeue whoever accepted, cool down whoever
        declined.

        **The requeue runs first.** Both orders are correct, and this one is
        chosen because the requeue is the part a player is waiting on: it
        puts them back in a pool that a scan may pick up within the second,
        while a cooldown only matters the next time its subject presses a
        button.
        """
        for ticket_id in handshake.accepted_ticket_ids:
            requeued = await self._queue.requeue(ticket_id=ticket_id)
            self._metrics.increment(
                ACCEPTANCE_FAILURE_ACTIONS,
                labels={
                    "action": (
                        AcceptanceFailureAction.REQUEUED
                        if requeued is not None
                        else AcceptanceFailureAction.REQUEUE_SKIPPED
                    )
                },
            )
            if requeued is not None:
                # Counted a second time, on purpose. The two metrics answer
                # different questions and §9 asks for the second: this one
                # is the **ticket funnel** — what became of a queue ticket
                # whose pairing did not end in a game — and `requeued` is a
                # member of it alongside `settled`, `released` and
                # `expired`. The counter above is the **policy's** view,
                # per player, and carries `requeue_skipped`, which has no
                # meaning in a funnel over tickets that moved.
                self._metrics.increment(
                    RECONCILIATION_ACTIONS,
                    labels={"action": ReconciliationAction.REQUEUED},
                )

        if handshake.declined_by_player_id is None:
            # A silent expiry. §3: silence is not a decline, so there is no
            # cooldown — and the counter records that the policy *considered*
            # this player and did nothing, which is the distinction §1 asks
            # to be kept visible.
            self._metrics.increment(
                ACCEPTANCE_FAILURE_ACTIONS,
                labels={"action": AcceptanceFailureAction.NO_ACTION},
            )
            return

        await self._cool_down(handshake.declined_by_player_id, match_id=handshake.match_id)

    async def _cool_down(self, player_id: UUID, *, match_id: UUID) -> None:
        """Bars the decliner from the queue, and records why — A64-015.6 §3.

        Its own transaction rather than the requeue's, and they are
        deliberately not one: the two writes are about **different
        players**, and a cooldown that could not be recorded must not undo a
        requeue that succeeded. A player left in the queue without their
        opponent being cooled down is a policy that under-applied by one
        window; rolling the requeue back would be a policy that punished the
        wrong person.

        The **audit row shares that transaction**, and that pairing is the
        one thing here that must not be relaxed: a bar with no record of why
        is precisely what A64-015.6 §3 exists to prevent, and two
        transactions would make it a crash away.

        `extended_existing` is read **before** the write rather than derived
        from it, and the difference is not cosmetic: comparing the stored
        expiry against the requested one only detects the case where the
        *old* bar outlasted the new one, which is the rarer half. A decline
        thirty seconds into a sixty-second window pushes the expiry out and
        leaves stored and requested identical — the ordinary repeat offender,
        and the one a support answer is actually about.

        So the check is "was a bar in force when this landed", answered by a
        primary-key read inside the same transaction. It costs one indexed
        lookup on a path that runs once per declined match. That is the fact
        A64-015.5's one-row-per-player enforcement discards, and the whole
        reason the audit relation is append-only.
        """
        at = self._clock.now()
        cooldown = QueueCooldown.after_decline(
            player_id, at=at, seconds=self._decline_cooldown_seconds
        )

        async with self._unit_of_work:
            # Inside the transaction, so the answer cannot be invalidated by
            # a concurrent decline between the read and the write.
            in_force = await self._cooldowns.active_for(player_id, now=at)
            stored = await self._cooldowns.apply(cooldown)
            await self._audit.record(
                CooldownRecord.of(
                    stored,
                    source_match_id=match_id,
                    applied_at=at,
                    extended_existing=in_force is not None,
                )
            )
            await self._unit_of_work.commit()

        self._metrics.increment(
            ACCEPTANCE_FAILURE_ACTIONS,
            labels={"action": AcceptanceFailureAction.COOLDOWN_APPLIED},
        )
        logger.info(
            "queue_cooldown_applied",
            extra={
                "player_id": str(player_id),
                "reason": stored.reason.value,
                # The window actually in force, which after an extension is
                # not the one configured — see `CooldownRepository.apply`.
                "remaining_seconds": stored.remaining(at),
            },
        )


def _parse(entry: OutboxEntry) -> FailedHandshake:
    """One outbox payload as the two facts the policy reads.

    Parsed from the **payload** rather than by re-reading the match, and
    that is the point of a self-contained event (AD-16): by the time this
    runs, retention may have removed the match, and the answer to "who
    accepted before it failed" would then be unavailable exactly when it is
    needed.

    Raises `KeyError` or `ValueError` for a payload this consumer cannot
    read, which the caller turns into a recorded per-entry failure. A
    malformed payload is a producer bug, and failing loudly on one entry is
    better than requeueing nobody and reporting success.
    """
    payload = entry.payload
    accepted: list[UUID] = []
    if bool(payload["light_accepted"]):
        accepted.append(UUID(str(payload["light_ticket_id"])))
    if bool(payload["dark_accepted"]):
        accepted.append(UUID(str(payload["dark_ticket_id"])))

    declined_by = (
        UUID(str(payload["player_id"])) if entry.event_type == MatchDeclined.event_type else None
    )
    return FailedHandshake(
        match_id=UUID(str(payload["match_id"])),
        accepted_ticket_ids=tuple(accepted),
        declined_by_player_id=declined_by,
    )


__all__ = [
    "CONSUMER_NAME",
    "SUBSCRIBED_EVENT_TYPES",
    "FailedHandshake",
    "MatchOutcomeService",
]
