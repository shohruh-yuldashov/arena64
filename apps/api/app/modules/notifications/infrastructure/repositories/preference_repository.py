"""The SQLAlchemy adapter for `application.ports.NotificationPreferenceRepository`.

Database-only, per repositories.md §2: this decides *how* to read and write,
never *whether* a change is legal. The legality is `domain.preference`'s and
is settled before anything here runs — which is what stops a locked
preference being written by a caller that forgot to ask.

## Two shapes of read, deliberately

`overrides_for` answers "show me this player's settings" — one player, every
pair. `permitted` answers "may I deliver to these people on this channel" —
many players, one channel. They are different indexes and different
questions, and one method serving both would make the delivery path fetch
rows it does not need per recipient (§11).
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.application.ports import DeliveryRequest
from app.modules.notifications.domain.preference import DeliveryChannel, default_enabled
from app.modules.notifications.domain.record import NotificationCategory
from app.modules.notifications.infrastructure.models import NotificationPreferenceModel

logger = logging.getLogger(__name__)


class SqlAlchemyNotificationPreferenceRepository:
    """Constructed per use case with the active session
    (repositories.md §5.1) — never holds one longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def overrides_for(
        self, user_id: UUID
    ) -> Mapping[tuple[NotificationCategory, DeliveryChannel], bool]:
        """Every override this player has stored. One query.

        A row whose category or channel this build no longer knows is
        **skipped** rather than raising. A vocabulary can shrink — a channel
        withdrawn, a category merged — and a settings screen that returned
        `500` because of a row nobody can see any more would be a worse
        answer than one that shows the current matrix.
        """
        rows = (
            await self._session.execute(
                select(NotificationPreferenceModel).where(
                    NotificationPreferenceModel.user_id == user_id
                )
            )
        ).scalars()

        overrides: dict[tuple[NotificationCategory, DeliveryChannel], bool] = {}
        for row in rows:
            key = _key_of(row.category, row.channel)
            if key is None:
                logger.warning(
                    "notification_preference_unknown_vocabulary",
                    extra={"category": row.category, "channel": row.channel},
                )
                continue
            overrides[key] = row.enabled
        return overrides

    async def replace(
        self,
        user_id: UUID,
        *,
        changes: Sequence[tuple[NotificationCategory, DeliveryChannel, bool]],
        at: datetime,
    ) -> None:
        """Upserts every change. One statement, whatever the batch size.

        `ON CONFLICT DO UPDATE` rather than a read followed by an insert or
        an update: two tabs saving at once must converge on one row per pair
        rather than one of them taking a unique violation, and neither may
        need to have read first (§8, §12).

        `created_at` keeps its original value on conflict — `excluded` is
        only applied to the two columns a rewrite actually changes, so "when
        did this player first depart from the default" survives every later
        edit.
        """
        if not changes:
            return

        statement = insert(NotificationPreferenceModel).values(
            [
                {
                    "user_id": user_id,
                    "category": category.value,
                    "channel": channel.value,
                    "enabled": enabled,
                    "created_at": at,
                    "updated_at": at,
                }
                for category, channel, enabled in changes
            ]
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="pk_notification_preference",
                set_={
                    "enabled": statement.excluded.enabled,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    async def permitted(
        self, requests: Sequence[DeliveryRequest], *, channel: DeliveryChannel
    ) -> frozenset[DeliveryRequest]:
        """The subset that may be delivered, in **one** query.

        The query fetches the overrides that exist for these recipients on
        this channel; everything absent falls to `default_enabled`. So the
        common case — a player who has never opened the settings screen —
        costs one row read that returns nothing, not a row per recipient.

        Deduplicated first: a batch can name the same recipient twice, and
        the `IN` list is the thing that grows with a tournament bracket.
        """
        if not requests:
            return frozenset()

        recipients = {request.recipient_id for request in requests}
        rows = (
            await self._session.execute(
                select(
                    NotificationPreferenceModel.user_id,
                    NotificationPreferenceModel.category,
                    NotificationPreferenceModel.enabled,
                ).where(
                    NotificationPreferenceModel.channel == channel.value,
                    NotificationPreferenceModel.user_id.in_(recipients),
                )
            )
        ).all()

        overrides = {
            (row.user_id, NotificationCategory(row.category)): row.enabled
            for row in rows
            if _is_category(row.category)
        }

        return frozenset(
            request
            for request in requests
            if overrides.get(
                (request.recipient_id, request.category),
                default_enabled(request.category, channel),
            )
        )


def _key_of(category: str, channel: str) -> tuple[NotificationCategory, DeliveryChannel] | None:
    """A stored pair as its enums, or `None` when this build does not know one."""
    try:
        return NotificationCategory(category), DeliveryChannel(channel)
    except ValueError:
        return None


def _is_category(value: str) -> bool:
    return value in {member.value for member in NotificationCategory}


__all__ = ["SqlAlchemyNotificationPreferenceRepository"]
