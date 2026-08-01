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
from app.modules.matchmaking.domain.queue_ticket import (
    QueueSnapshot,
    QueueStatus,
    QueueTicket,
    QueueType,
    Region,
)
from app.modules.matchmaking.infrastructure.models import QueueTicketModel

logger = logging.getLogger(__name__)

#: The one constraint this adapter translates. Keyed by the name declared in
#: `models.py.__table_args__` — renaming it there without renaming it here
#: silently stops the translation working, which is why a contract test
#: drives a real violation through this path.
_ONE_LIVE_PER_PLAYER_INDEX = "uq_queue_ticket__one_live_per_player"


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
            queue_type=row.queue_type,
            region=row.region,
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
                QueueTicketModel.status == QueueStatus.WAITING,
                QueueTicketModel.expires_at > now,
            )
            .limit(1)
        )
        return self._to_domain(row) if row is not None else None

    async def queue_snapshot(
        self,
        *,
        queue_type: QueueType,
        region: Region,
        now: datetime,
        limit: int,
    ) -> QueueSnapshot:
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
            QueueTicketModel.queue_type == queue_type,
            QueueTicketModel.region == region,
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
            queue_type=queue_type,
            region=region,
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
                QueueTicketModel.status == QueueStatus.WAITING,
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
                    QueueTicketModel.status == QueueStatus.WAITING,
                )
                .values(status=QueueStatus.EXPIRED, resolved_at=at)
            ),
        )
        return int(result.rowcount)

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
