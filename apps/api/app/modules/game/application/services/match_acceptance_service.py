"""`MatchAcceptanceService` — the three answers a paired player can give,
and the sweep that answers for the ones who give none.

Implements `game.public.MatchAcceptanceUseCase` and
`game.public.MatchAcceptanceExpiryUseCase`. Orchestrates; does not decide:
which transitions are legal is `MatchRecord`'s, who may respond is derived
from the caller's own identifier, and what is left here is the lock, the
transaction and the event that rides in it.

## Every write takes the row lock first

`accept` and `decline` both `SELECT ... FOR UPDATE` before they read
anything they will act on. That is not defensive: two players accepting at
the same instant is the *expected* traffic on this path, not a rare race,
and without the lock both would read "the opponent has not accepted" and
both would write a match that is still pending. The lock makes the second
one read what the first wrote and activate.

`FOR UPDATE` rather than `SKIP LOCKED`, which is the opposite of every
other claim on this platform — see `MatchRecordRepository.lock` for why a
player has nowhere else to go.

## Absence and refusal answer the same way

`MatchNotFound` is raised for a match that does not exist **and** for one
the caller is not in. A distinct status for the second would make live
match identifiers enumerable, so the difference reaches the log and never
the wire (CLAUDE.md §9.7).
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.engine import PlayerSide
from app.modules.game.application.ports import MatchRecordRepository
from app.modules.game.domain.events import (
    MatchAcceptanceExpired,
    MatchAcceptedByPlayer,
    MatchActivated,
    MatchDeclined,
)
from app.modules.game.domain.exceptions import MatchNotFound, NotAMatchParticipant
from app.modules.game.domain.match_record import MatchRecord, MatchRecordStatus
from app.modules.game.public.acceptance import PendingMatchView
from app.modules.game.public.metrics import (
    MATCH_ANSWER_LATENCY,
    MATCH_OUTCOMES,
    AnswerLatency,
    MatchOutcome,
)
from app.platform.events import DomainEvent
from app.platform.metrics import MetricsRecorder
from app.platform.outbox import EventPublisher

logger = logging.getLogger(__name__)


class MatchAcceptanceService:
    """The acceptance use cases, over one session."""

    def __init__(
        self,
        *,
        matches: MatchRecordRepository,
        events: EventPublisher,
        unit_of_work: UnitOfWork,
        clock: Clock,
        metrics: MetricsRecorder,
    ) -> None:
        self._matches = matches
        self._events = events
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._metrics = metrics

    async def pending_match(self, player_id: UUID) -> PendingMatchView | None:
        """The match this player must answer, or `None`.

        Read-only; opens no transaction and takes no lock. Scoped to the
        caller by construction — there is no parameter that could name
        another player's match, which is why this needs no ownership check.
        """
        record = await self._matches.pending_for(player_id)
        return None if record is None else view_of(record, player_id)

    async def accept(self, *, player_id: UUID, match_id: UUID) -> PendingMatchView:
        """Records this player's acceptance, activating the match if it is
        the second one.

        One transaction, so the transition and the event it announces are
        one fact (AD-16). Two events are possible and exactly one is
        published: `match_accepted_by_player` while the opponent is still
        silent, `match_activated` when they are not — see
        `MatchAcceptedByPlayer` on why never both.
        """
        return await self._respond(player_id=player_id, match_id=match_id, accepting=True)

    async def decline(self, *, player_id: UUID, match_id: UUID) -> PendingMatchView:
        """Records this player's refusal and cancels the match."""
        return await self._respond(player_id=player_id, match_id=match_id, accepting=False)

    async def _respond(
        self, *, player_id: UUID, match_id: UUID, accepting: bool
    ) -> PendingMatchView:
        """The body both answers share.

        One method rather than two, because the lock, the participation
        check, the compare-and-set and the transaction are identical and
        only the transition differs — three copies of the locking sequence
        is three chances to omit the part that makes it safe.
        """
        at = self._clock.now()

        async with self._unit_of_work:
            record = await self._matches.lock(match_id)
            if record is None:
                await self._unit_of_work.rollback()
                raise MatchNotFound("That match does not exist.")

            try:
                side = record.side_of(player_id)
            except NotAMatchParticipant:
                await self._unit_of_work.rollback()
                # The *operator* learns which of the two happened; the
                # caller cannot, or match ids become enumerable.
                logger.warning(
                    "match_response_by_non_participant",
                    extra={"match_id": str(match_id), "player_id": str(player_id)},
                )
                raise MatchNotFound("That match does not exist.") from None

            # Raises `MatchNotPending` or `AcceptanceWindowClosed` out of
            # the `async with`, which rolls the transaction back — nothing
            # has been written at this point, so there is nothing to undo
            # beyond releasing the lock.
            answered = (
                record.accepted_by(side, at=at) if accepting else record.declined(side, at=at)
            )

            if answered is record:
                # A repeat acceptance from a player who already accepted an
                # already-active match. Nothing changed, so nothing is
                # written and nothing is published — see
                # `MatchRecord.accepted_by` on why this is the honest
                # answer rather than a `409`.
                await self._unit_of_work.rollback()
                return view_of(record, player_id)

            if not await self._matches.settle(answered):
                # Unreachable while the row lock above holds, and checked
                # anyway: the day the two are separated, a silent
                # last-write-wins is a match two people accepted being
                # recorded as declined.
                await self._unit_of_work.rollback()
                logger.error(
                    "match_response_lost",
                    extra={"match_id": str(match_id), "side": side.value},
                )
                raise MatchNotFound("That match does not exist.")

            for event in _events_for(answered, side=side, player_id=player_id, accepting=accepting):
                await self._events.publish(event)
            await self._unit_of_work.commit()

        self._record_answer(record, answered, at=at, accepting=accepting)
        logger.info(
            "match_answered",
            extra={
                "match_id": str(answered.id),
                "pairing_id": str(answered.pairing_id),
                "side": side.value,
                "accepted": accepting,
                "status": answered.status.value,
            },
        )
        return view_of(answered, player_id)

    def _record_answer(
        self, before: MatchRecord, after: MatchRecord, *, at: datetime, accepting: bool
    ) -> None:
        """A64-015.5 §7 — the measurement the deadline must be tuned from.

        Taken **after** the commit, so a rolled-back answer is never
        counted, and from the injected clock rather than a second reading,
        so the latency is measured against the instant the transition was
        actually stamped with.

        `first_response` fires when neither side had answered before this
        one. It is a property of the *match* rather than of the player, so
        it is recorded once per pairing however many answers follow — which
        is what makes its count comparable with `match_outcomes_total`.
        """
        latency = (at - after.created_at).total_seconds()
        if not (before.light.has_accepted or before.dark.has_accepted):
            self._metrics.observe(
                MATCH_ANSWER_LATENCY, latency, labels={"outcome": AnswerLatency.FIRST_RESPONSE}
            )

        if not accepting:
            self._metrics.observe(
                MATCH_ANSWER_LATENCY, latency, labels={"outcome": AnswerLatency.DECLINED}
            )
            self._metrics.increment(MATCH_OUTCOMES, labels={"outcome": MatchOutcome.DECLINED})
            return

        if after.status is MatchRecordStatus.ACTIVE:
            self._metrics.observe(
                MATCH_ANSWER_LATENCY, latency, labels={"outcome": AnswerLatency.BOTH_ACCEPTED}
            )
            self._metrics.increment(MATCH_OUTCOMES, labels={"outcome": MatchOutcome.BOTH_ACCEPTED})

    def _record_outcome(self, record: MatchRecord, *, outcome: MatchOutcome, at: datetime) -> None:
        """An ending nobody answered for — the expiry sweep's half of §7.

        The latency recorded is to the **deadline**, not to the instant the
        sweep noticed: how late the reconciler was is the job's property,
        and mixing it into this histogram would make the tail a measure of
        the scheduler rather than of the players. See `MatchAcceptanceExpired`
        on the same choice for `occurred_at`.
        """
        latency = (record.acceptance_deadline - record.created_at).total_seconds()
        self._metrics.observe(
            MATCH_ANSWER_LATENCY, latency, labels={"outcome": AnswerLatency.EXPIRED}
        )
        self._metrics.increment(MATCH_OUTCOMES, labels={"outcome": outcome})

    async def expire_overdue(self, *, limit: int) -> Sequence[UUID]:
        """Expires up to `limit` pending matches whose window has closed.

        Two transactions, for the reason `QueueService.expire_due` uses
        two: the claim commits on its own so the rows this worker took are
        visibly locked before anything else happens, and a second
        reconciler polling mid-batch skips them.

        Never raises. This runs from a scheduled task, and a sweep that
        propagated would stop the schedule — the argument
        `OutboxRelay.run_once` and `PresenceSweeper.sweep_once` both make.
        """
        now = self._clock.now()

        async with self._unit_of_work:
            claimed = await self._matches.claim_overdue(now=now, limit=limit)
            await self._unit_of_work.commit()

        if not claimed:
            return ()

        expired: list[UUID] = []
        try:
            async with self._unit_of_work:
                for record in claimed:
                    settled = record.expired(now)
                    if not await self._matches.settle(settled):
                        # Somebody answered between the claim and this
                        # write. Not a failure — their answer stands, and
                        # this match simply is not expired.
                        continue
                    expired.append(settled.id)
                    self._record_outcome(settled, outcome=MatchOutcome.EXPIRED, at=now)
                    await self._events.publish(
                        MatchAcceptanceExpired(
                            # The match's own deadline, not the sweep's
                            # instant — the fact became true when the
                            # window closed, and the outbox orders by
                            # `occurred_at` (database.md §12.5).
                            occurred_at=settled.acceptance_deadline,
                            match_id=settled.id,
                            pairing_id=settled.pairing_id,
                            light_player_id=settled.light.player_id,
                            dark_player_id=settled.dark.player_id,
                            light_ticket_id=settled.light.queue_ticket_id,
                            dark_ticket_id=settled.dark.queue_ticket_id,
                            light_accepted=settled.light.has_accepted,
                            dark_accepted=settled.dark.has_accepted,
                        )
                    )
                await self._unit_of_work.commit()
        except Exception as error:  # noqa: BLE001 — a background sweep must not escalate
            # Nothing is lost: the matches are still pending and still
            # overdue, so the next tick claims them again.
            logger.error(
                "match_acceptance_expiry_failed",
                extra={"claimed": len(claimed), "error": type(error).__name__},
                exc_info=error,
            )
            return ()

        # One line for the batch rather than one per match — a backlog
        # would otherwise bury whatever else was happening (CLAUDE.md §8.8).
        logger.info(
            "match_acceptance_expired",
            extra={"claimed": len(claimed), "expired": len(expired)},
        )
        return tuple(expired)


def _events_for(
    record: MatchRecord, *, side: PlayerSide, player_id: UUID, accepting: bool
) -> tuple[DomainEvent, ...]:
    """The one event this answer produced.

    A tuple rather than a single value because the shape reads as "what
    this transition announces", and because an activation that later also
    needs to announce a created `Match` adds an entry here rather than a
    branch at the call site.
    """
    # `Any`-valued rather than inferred, because the two ticket ids are
    # nullable since A64-019.6 and every other entry is not — an inferred
    # `dict[str, UUID | None]` would then widen the four that are never
    # absent. The events' own signatures are where each field's nullability
    # is stated; this mapping only carries them.
    identity: dict[str, Any] = {
        "match_id": record.id,
        "pairing_id": record.pairing_id,
        "light_player_id": record.light.player_id,
        "dark_player_id": record.dark.player_id,
        "light_ticket_id": record.light.queue_ticket_id,
        "dark_ticket_id": record.dark.queue_ticket_id,
    }
    if not accepting:
        return (
            MatchDeclined(
                occurred_at=_settled_at(record),
                **identity,
                side=side,
                player_id=player_id,
                # Which sides had said yes when the refusal landed. At most
                # one is `True` — see `MatchDeclined` — and it names the
                # player A64-015.5 §1 owes a requeue to.
                light_accepted=record.light.has_accepted,
                dark_accepted=record.dark.has_accepted,
            ),
        )
    if record.status is MatchRecordStatus.ACTIVE:
        return (
            MatchActivated(
                occurred_at=_settled_at(record),
                **identity,
                variant=record.variant,
                rated=record.rated,
            ),
        )
    accepted_at = record.seat(side).accepted_at
    return (
        MatchAcceptedByPlayer(
            occurred_at=accepted_at if accepted_at is not None else record.created_at,
            **identity,
            side=side,
            player_id=player_id,
        ),
    )


def _settled_at(record: MatchRecord) -> datetime:
    """When the handshake ended.

    `settled_at` is non-null for every record that reaches here — the
    transitions that produce a decline or an activation set it — and the
    fallback keeps this total rather than making a settled record's event
    depend on an `assert`.
    """
    return record.settled_at if record.settled_at is not None else record.created_at


def view_of(record: MatchRecord, player_id: UUID) -> PendingMatchView:
    """`MatchRecord` as the participant `player_id` may see it.

    A free function rather than a method on the record, because it is a
    *presentation* of the aggregate for one reader and the aggregate has no
    business knowing there is a wire. It lives here rather than in
    `public/` so that the published package stays types and contracts with
    no logic — the shape `friends/public/` already keeps.

    Raises `NotAMatchParticipant` for anybody else, which is what makes it
    impossible to build a view of a match for a player who is not in it.
    """
    side = record.side_of(player_id)
    you = record.seat(side)
    opponent = record.seat(side.opponent())
    return PendingMatchView(
        match_id=record.id,
        status=record.status,
        your_side=side,
        opponent_player_id=opponent.player_id,
        variant=record.variant,
        rated=record.rated,
        acceptance_deadline=record.acceptance_deadline,
        you_accepted=you.has_accepted,
        opponent_accepted=opponent.has_accepted,
        created_at=record.created_at,
    )


__all__ = ["MatchAcceptanceService", "view_of"]
