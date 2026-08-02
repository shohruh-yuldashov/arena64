"""The SQLAlchemy adapter for `application.ports.CooldownRepository` —
A64-015.5 §3.

Database-only (repositories.md §2): whether a player *deserves* a cooldown
is `MatchOutcomeService`'s question, how long is configuration, and what is
left here is one upsert, one filtered read and one bounded delete.

## The upsert is the whole file

    INSERT ... ON CONFLICT (player_id) DO UPDATE
        SET expires_at = GREATEST(excluded.expires_at, queue_cooldown.expires_at)

That statement is what makes §3's "repeated decline does not bypass the
cooldown" true under concurrency. The obvious alternative — read the
existing row, take the later expiry in Python, write it back — is correct
until two declines by one player land in two workers at the same instant, at
which point both read "no cooldown" and the second overwrites the first
rather than extending it. A player who declined twice would then serve one
cooldown.

`GREATEST` rather than an unconditional overwrite for the same reason in
reverse: a *shorter* cooldown arriving second (a smaller configured window
after a deploy, or an out-of-order redelivery) must not shorten one already
in force.

`created_at` is deliberately **not** in the `SET` clause. The question it
answers is "when did this player start being barred", and a second decline
does not restart that — the same rule `QueueCooldown.extended_to` states in
the domain, applied here.
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.matchmaking.domain.cooldown import QueueCooldown
from app.modules.matchmaking.infrastructure.models import QueueCooldownModel

logger = logging.getLogger(__name__)


class SqlAlchemyCooldownRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1) — never holds a session longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: QueueCooldownModel) -> QueueCooldown:
        return QueueCooldown(
            player_id=row.player_id,
            reason=row.reason,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )

    async def apply(self, cooldown: QueueCooldown) -> QueueCooldown:
        """Records a cooldown, extending any the player already has.

        Returns what is now in force, read back from the statement's
        `RETURNING` rather than assumed — a caller reporting `retry_after`
        must report the *stored* window, and after an extension that is not
        the one it passed in.

        **Flushes, never commits.** The caller's unit of work spans the
        cooldown and the outbox row that records the decline which caused
        it: one transaction, because a cooldown for a decline that rolled
        back is a player barred for nothing (AD-16).
        """
        statement = (
            insert(QueueCooldownModel)
            .values(
                player_id=cooldown.player_id,
                reason=cooldown.reason,
                expires_at=cooldown.expires_at,
                created_at=cooldown.created_at,
            )
            .on_conflict_do_update(
                index_elements=[QueueCooldownModel.player_id],
                set_={
                    "expires_at": func.greatest(
                        QueueCooldownModel.expires_at,
                        # `excluded` is the row this statement tried to
                        # insert. Naming it explicitly rather than using the
                        # Python value keeps the comparison inside the
                        # statement, which is what makes it atomic.
                        insert(QueueCooldownModel).excluded.expires_at,
                    ),
                    "reason": insert(QueueCooldownModel).excluded.reason,
                },
            )
            .returning(QueueCooldownModel)
        )

        row = (await self._session.scalars(statement)).one()
        await self._session.flush()
        return self._to_domain(row)

    async def active_for(self, player_id: UUID, *, now: datetime) -> QueueCooldown | None:
        """The cooldown barring this player, or `None`.

        `expires_at > now` is applied **in the query**, exactly as
        `active_ticket` applies its own deadline and for the same reason: a
        lapsed row that retention has not reached yet must read as absent,
        or a player would be refused by bookkeeping rather than by a rule.

        A single-row lookup by primary key — the cheapest read this module
        issues, which matters because it is on the queue-join path.
        """
        row = await self._session.scalar(
            select(QueueCooldownModel).where(
                QueueCooldownModel.player_id == player_id,
                QueueCooldownModel.expires_at > now,
            )
        )
        return self._to_domain(row) if row is not None else None

    async def prune_expired(self, *, before: datetime, batch_size: int) -> int:
        """Deletes lapsed cooldowns. Returns how many rows went.

        Bounded, and safe for more than one pruner by the same
        `FOR UPDATE SKIP LOCKED` the outbox's retention uses: the ids are
        selected and locked first, then deleted by key, so two workers
        running together delete disjoint sets instead of deadlocking on an
        unhinted `DELETE ... LIMIT`.

        The horizon is `expires_at` rather than `created_at`: a cooldown is
        worthless the moment it lifts, unlike a resolved queue ticket, so
        there is no window to preserve beyond the read that might be
        in flight.
        """
        lapsed = (
            select(QueueCooldownModel.player_id)
            .where(QueueCooldownModel.expires_at <= before)
            .order_by(QueueCooldownModel.expires_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        claimed = list((await self._session.scalars(lapsed)).all())
        if not claimed:
            return 0

        await self._session.execute(
            delete(QueueCooldownModel).where(QueueCooldownModel.player_id.in_(claimed))
        )
        return len(claimed)


__all__ = ["SqlAlchemyCooldownRepository"]
