"""The SQLAlchemy adapter for `application.ports.QueueRepository`.

Database-only (repositories.md §2): it decides *how* to store and fetch,
never *whether*. Every "is this allowed" question is answered by
`QueueTicket` and `QueueService`.

## The claim is the only interesting statement in this file

Everything else is an insert, an update by primary key, or a filtered read.
`claim_due` is where "support future horizontal workers" (A64-014.1) is
either true or a comment, and the shape that makes it true is the one the
outbox already proved:

    SELECT ... FOR UPDATE SKIP LOCKED

A64-014.1 requires exactly this and forbids inventing an alternative, and
`app/platform/outbox/repository.py` already records why each alternative is
worse — an unhinted `UPDATE ... RETURNING` serialises N workers into one, an
optimistic version column pays a wasted read plus a retry per row per tick,
and a table-level advisory lock makes horizontal scaling impossible by
construction.

What is different here, and worth stating because it looks like an
omission: **there is no `claimed_by` column.** The outbox has one and this
table does not, deliberately. It is diagnostic there — "correctness comes
from `FOR UPDATE SKIP LOCKED`, not from this column, and a design that
relied on it would be a lease with no way to detect a dead holder" — and a
diagnostic column on a relation whose rows live ten minutes buys less than
the log line that carries the same identifier. The worker id reaches
`queue_expired` instead.

## Two writes are compare-and-set, and both races are ordinary

`cancel` and `expire` carry `status = 'waiting'` in their predicates. The
races are a player cancelling on two devices, and a player cancelling as the
expiry sweep commits — neither is exotic, and without the predicate the
later write silently overwrites the earlier transition, so a ticket would
report `cancelled` for something that expired or the reverse. Once
A64-014.2's pairing exists the same predicate is what stops a cancellation
overwriting a `matched` ticket whose match has already been created.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.matchmaking.domain.exceptions import AlreadyQueued
from app.modules.matchmaking.domain.queue_pool import QueuePool
from app.modules.matchmaking.domain.queue_ticket import QueueSnapshot, QueueStatus, QueueTicket
from app.modules.matchmaking.infrastructure.models import QueueTicketModel

logger = logging.getLogger(__name__)

#: The one constraint this adapter translates. Keyed by the name declared in
#: `models.py.__table_args__` — renaming it there without renaming it here
#: silently stops the translation working, which is why a contract test
#: drives a real violation through this path.
_ONE_LIVE_PER_PLAYER_INDEX = "uq_queue_ticket__one_live_per_player"

#: The two statuses that mean "this player is still in the queue".
#:
#: Mirrors `QueueStatus.is_live` and is derived from it, so a sixth status
#: cannot leave this predicate saying something different from the three
#: database predicates in `models.py`.
_LIVE_STATUSES = tuple(status for status in QueueStatus if status.is_live)


class SqlAlchemyQueueRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1) — never holds a session longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: QueueTicketModel) -> QueueTicket:
        return QueueTicket(
            id=row.id,
            player_id=row.player_id,
            pool=QueuePool(variant=row.variant, queue_type=row.queue_type, region=row.region),
            rating_snapshot=row.rating_snapshot,
            entered_at=row.entered_at,
            expires_at=row.expires_at,
            status=row.status,
            resolved_at=row.resolved_at,
        )

    async def enqueue(self, ticket: QueueTicket) -> QueueTicket:
        """Persists a new waiting ticket.

        **Flushes, never commits.** The caller's unit of work spans the
        ticket and the outbox row that announces it: one transaction,
        because an event for a ticket that rolled back is a pairing worker
        acting on somebody who is not in the queue (AD-16).

        Raises `AlreadyQueued` when the partial unique index refuses a
        second live ticket. The flush is what makes that surface *here*,
        where it can be translated, rather than at the commit inside the
        service's `async with` — which would escape as a raw
        `IntegrityError` and become a 500.
        """
        row = QueueTicketModel(
            id=ticket.id,
            player_id=ticket.player_id,
            variant=ticket.pool.variant,
            queue_type=ticket.queue_type,
            region=ticket.region,
            rating_snapshot=ticket.rating_snapshot,
            entered_at=ticket.entered_at,
            expires_at=ticket.expires_at,
            status=ticket.status,
            resolved_at=ticket.resolved_at,
        )
        self._session.add(row)

        try:
            await self._session.flush()
        except IntegrityError as error:
            raise self._translate(error) from error

        return self._to_domain(row)

    async def cancel(self, ticket: QueueTicket) -> bool:
        """Writes a resolved ticket, only if the row is still `waiting`.

        Returns whether it applied — see this module's docstring on the two
        races the predicate closes.

        The status written is the *ticket's own*, not a literal
        `cancelled`: this method persists whatever terminal state the
        aggregate produced, so `QueueTicket.matched` needs no second method
        here when A64-014.2 arrives. It is named `cancel` because that is
        the operation A64-014.1 specifies and the only caller today.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(QueueTicketModel)
                .where(
                    QueueTicketModel.id == ticket.id,
                    QueueTicketModel.status == QueueStatus.WAITING,
                )
                .values(status=ticket.status, resolved_at=ticket.resolved_at)
            ),
        )
        return int(result.rowcount) == 1

    async def active_ticket(self, player_id: UUID, *, now: datetime) -> QueueTicket | None:
        """The player's live ticket, or `None`.

        `status = 'waiting' AND expires_at > now` — the deadline is applied
        in the query rather than by the caller, so every reader agrees about
        what "queued" means and none of them can forget the second half.
        See the port on why a due-but-unswept ticket must read as absent.

        Served by `uq_queue_ticket__one_live_per_player`, which is why this
        table needs no separate index on `player_id`: the constraint that
        enforces QT-1 is also the index that answers the question QT-1 is
        about.
        """
        row = await self._session.scalar(
            select(QueueTicketModel)
            .where(
                QueueTicketModel.player_id == player_id,
                QueueTicketModel.status.in_(_LIVE_STATUSES),
                QueueTicketModel.expires_at > now,
            )
            .limit(1)
        )
        return self._to_domain(row) if row is not None else None

    async def queue_snapshot(self, *, pool: QueuePool, now: datetime, limit: int) -> QueueSnapshot:
        """One pool's depth and its oldest live tickets.

        **Two statements, and they are not an N+1.** The count and the page
        answer different questions over the same predicate — how many are
        waiting, and which ones to look at first — and a single query
        returning both would either window-function the count onto every row
        or truncate it to the page size. Two indexed reads against
        `ix_queue_ticket__pool` is the cheap, honest shape; what CLAUDE.md
        §10.4 forbids is a query *per item*, which no arrangement here
        produces.

        The count is an index-only scan over waiting rows in one pool, so it
        is bounded by concurrency rather than by history — the partial index
        is what makes counting affordable at all.
        """
        live = (
            QueueTicketModel.variant == pool.variant,
            QueueTicketModel.queue_type == pool.queue_type,
            QueueTicketModel.region == pool.region,
            QueueTicketModel.status == QueueStatus.WAITING,
            QueueTicketModel.expires_at > now,
        )

        waiting = await self._session.scalar(
            select(func.count()).select_from(QueueTicketModel).where(*live)
        )

        rows = await self._session.scalars(
            select(QueueTicketModel)
            .where(*live)
            .order_by(QueueTicketModel.entered_at, QueueTicketModel.id)
            .limit(limit)
        )

        return QueueSnapshot(
            pool=pool,
            taken_at=now,
            waiting=int(waiting or 0),
            tickets=tuple(self._to_domain(row) for row in rows),
        )

    async def claim_due(
        self, *, now: datetime, limit: int, claimed_by: str
    ) -> Sequence[QueueTicket]:
        """Takes up to `limit` due tickets for this worker. See the module
        docstring on `SKIP LOCKED`.

        "Due" is two conditions, and each excludes a different row:

            status = 'waiting'   not already resolved — a cancelled ticket
                                 must not be expired on top of its
                                 cancellation
            expires_at <= now    the window has actually closed

        Ordered by `expires_at` so a backlog drains in deadline order,
        which is what makes each `QueueTicketExpired` event's `occurred_at`
        agree with the order the relay publishes them in (database.md
        §12.5). Served by `ix_queue_ticket__due`, whose predicate matches the
        first condition exactly.

        **Claiming is not a transition.** The rows come back `waiting` and
        stay that way until `expire` runs; the lock is what excludes another
        worker, and it lasts as long as the caller's transaction. A worker
        that dies here leaves tickets the next sweep claims again.

        `claimed_by` is logged rather than written — see the module
        docstring on the absent column.
        """
        due = (
            select(QueueTicketModel.id)
            .where(
                QueueTicketModel.status.in_(_LIVE_STATUSES),
                QueueTicketModel.expires_at <= now,
            )
            .order_by(QueueTicketModel.expires_at, QueueTicketModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

        claimed_ids = list((await self._session.scalars(due)).all())
        if not claimed_ids:
            return ()

        logger.debug(
            "queue_tickets_claimed",
            extra={"claimed": len(claimed_ids), "worker_id": claimed_by},
        )

        rows = await self._session.scalars(
            select(QueueTicketModel)
            .where(QueueTicketModel.id.in_(claimed_ids))
            .order_by(QueueTicketModel.expires_at, QueueTicketModel.id)
        )
        return [self._to_domain(row) for row in rows]

    async def expire(self, ticket_ids: Sequence[UUID], *, at: datetime) -> int:
        """One statement for the whole sweep, whatever the batch size.

        `status = 'waiting'` in the predicate as well as the id list, so a
        ticket the player cancelled between this worker's claim and its
        commit is not re-stamped as expired — and the count returned then
        reflects what this worker actually did rather than what it intended.
        """
        if not ticket_ids:
            return 0

        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(QueueTicketModel)
                .where(
                    QueueTicketModel.id.in_(ticket_ids),
                    QueueTicketModel.status.in_(_LIVE_STATUSES),
                )
                .values(status=QueueStatus.EXPIRED, resolved_at=at)
            ),
        )
        return int(result.rowcount)

    async def claim_pair(
        self, ticket_ids: Sequence[UUID], *, now: datetime
    ) -> Sequence[QueueTicket]:
        """Locks exactly these two tickets, or neither — A64-015.3 §7.

        `SELECT ... FOR UPDATE SKIP LOCKED`, the same mechanism `claim_due`
        uses and the one the outbox proved. Nothing new is invented here,
        which is the point: A64-015.3 forbids a distributed lock, and the
        alternatives are already argued against in this module's docstring.

        **Both or nothing**, enforced by a count rather than by hope. Two
        workers that selected the same pair race here; the loser's `SELECT`
        skips at least one locked row, comes back short, and returns
        nothing. Returning the one row it did lock would hand the caller a
        half-claim and, worse, hold a lock on a ticket that is about to be
        reserved for somebody else.

        The `expires_at > now` clause is not redundant with the snapshot
        that produced these ids: a ticket can fall due between the read and
        the claim, and pairing somebody whose window has closed is exactly
        the "match created for a player who left" that
        `MATCHMAKING_TICKET_TTL_SECONDS` exists to bound.
        """
        if len(ticket_ids) != 2:
            raise ValueError("a pairing claim is exactly two tickets")

        locked = (
            select(QueueTicketModel.id)
            .where(
                QueueTicketModel.id.in_(ticket_ids),
                QueueTicketModel.status == QueueStatus.WAITING,
                QueueTicketModel.expires_at > now,
            )
            .with_for_update(skip_locked=True)
        )

        claimed_ids = list((await self._session.scalars(locked)).all())
        if len(claimed_ids) != 2:
            return ()

        rows = await self._session.scalars(
            select(QueueTicketModel).where(QueueTicketModel.id.in_(claimed_ids))
        )
        return [self._to_domain(row) for row in rows]

    async def reserve(self, tickets: Sequence[QueueTicket]) -> bool:
        """`waiting -> reserved` for every ticket, or none of them."""
        return await self._transition(
            tickets, expected=QueueStatus.WAITING, status=QueueStatus.RESERVED, resolved_at=None
        )

    async def release(self, tickets: Sequence[QueueTicket]) -> bool:
        """`reserved -> waiting` — the compensation.

        `entered_at` is not in the `SET` clause, so a released player keeps
        the place in line they held. That is not an omission to be tidied
        up later: see `QueueTicket.released` on why a platform failure must
        not cost a player their wait.
        """
        return await self._transition(
            tickets, expected=QueueStatus.RESERVED, status=QueueStatus.WAITING, resolved_at=None
        )

    async def complete(self, tickets: Sequence[QueueTicket], *, at: datetime) -> bool:
        """`reserved -> matched`, with the instant the match was created."""
        return await self._transition(
            tickets, expected=QueueStatus.RESERVED, status=QueueStatus.MATCHED, resolved_at=at
        )

    async def _transition(
        self,
        tickets: Sequence[QueueTicket],
        *,
        expected: QueueStatus,
        status: QueueStatus,
        resolved_at: datetime | None,
    ) -> bool:
        """One compare-and-set over a set of tickets. All or nothing.

        One statement rather than one per ticket, so a pair cannot half
        apply within a transaction that then commits. `expected` is in the
        predicate for the reason every write in this file carries one: a
        blind `UPDATE` would let a pairing overwrite a cancellation, or
        resurrect a ticket the expiry sweep had already resolved.

        Returns whether **every** ticket moved, and writes nothing at all
        when the answer is no.

        ## The guard subquery, and why counting the rowcount is not enough

        The obvious shape — update the matching rows, compare `rowcount` to
        the batch size — reports "all or nothing" and does not deliver it:
        with one ticket already cancelled, the *other* one moves and the
        method returns `False`. A caller that rolled back would be fine and
        a caller that did not would have half-reserved a pairing, which
        strands one player with no match coming.

        So the statement gates itself: it applies only if the number of
        rows still in `expected` equals the number asked for. PostgreSQL
        evaluates the subquery against the statement's own snapshot, so it
        cannot see a partial application of the update it is guarding.

        The three public transitions differ only in these three arguments,
        so they share a body: three copies would be three chances to forget
        the predicate, which is the one clause that makes any of them safe.
        """
        if not tickets:
            return True

        ticket_ids = [ticket.id for ticket in tickets]
        movable = (
            select(func.count())
            .select_from(QueueTicketModel)
            .where(
                QueueTicketModel.id.in_(ticket_ids),
                QueueTicketModel.status == expected,
            )
            .scalar_subquery()
        )

        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(QueueTicketModel)
                .where(
                    QueueTicketModel.id.in_(ticket_ids),
                    QueueTicketModel.status == expected,
                    movable == len(ticket_ids),
                )
                .values(status=status, resolved_at=resolved_at)
            ),
        )
        return int(result.rowcount) == len(tickets)

    @staticmethod
    def _translate(error: IntegrityError) -> Exception:
        """A driver `IntegrityError` as the typed exception the service
        would have raised.

        Only the uniqueness index is translated. The three CHECK
        constraints are unreachable from the application — `QueueTicket`
        refuses to construct in any of those shapes — so a violation means a
        row was written by something other than this code path, and the
        honest outcome is the generic 500 an untranslated `IntegrityError`
        becomes.
        """
        constraint = getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)
        if constraint == _ONE_LIVE_PER_PLAYER_INDEX:
            return AlreadyQueued("You are already in a matchmaking queue.")
        return error
