"""The SQLAlchemy adapter for `application.ports.PushSubscriptionRepository`.

Database-only, per repositories.md §2. It decides *how* a subscription is
stored and revoked, never *whether* one may be — that is
`PushSubscriptionService`'s, and it is settled before anything here runs.

## The upsert is the ownership rule — A64-021.6 §23

`ON CONFLICT (endpoint) DO UPDATE` is not an optimisation. It is the
cross-account leakage defence, expressed as one statement:

    a browser re-subscribing with the same endpoint keeps working
    a browser whose endpoint now belongs to a different account is
      **re-bound to that account**, in the same statement, with no window
      in which two rows claim it

Read-then-write would have that window, and the row that lost the race
would be the one still pointing at the previous user. The unique constraint
is what makes this possible, and it spans revoked rows as well as live ones
— a revoked row reappearing as a second live one would push twice to one
browser.
"""

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.domain.subscription import PushSubscription
from app.modules.notifications.infrastructure.models import PushSubscriptionModel


class SqlAlchemyPushSubscriptionRepository:
    """Constructed per unit of work with the active session
    (repositories.md §5.1) — never holds one longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, subscription: PushSubscription) -> PushSubscription:
        """Stores a browser's subscription, or takes over an existing one.

        Returns the **stored** row, whose `id` is the pre-existing one on a
        takeover. Callers must use what comes back rather than what they
        passed in: a delivery row keyed on a discarded id would point at
        nothing.

        `created_at` is deliberately not updated on conflict. It records
        when this *browser* first subscribed, which is what an operator
        asking "how long has this device been here" means — where the
        `user_id` and `updated_at` do move, because ownership did.

        `revoked_at` is cleared, which is the case that matters most: a
        browser that was signed out of and is now signing back in must
        become deliverable again rather than staying dead because a row
        already existed.
        """
        statement = insert(PushSubscriptionModel).values(
            id=subscription.id,
            user_id=subscription.user_id,
            endpoint=subscription.endpoint,
            p256dh=subscription.p256dh,
            auth=subscription.auth,
            created_at=subscription.created_at,
            updated_at=subscription.updated_at,
            last_seen_at=subscription.last_seen_at,
            revoked_at=None,
        )
        stored = await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_push_subscription__endpoint",
                set_={
                    "user_id": statement.excluded.user_id,
                    # The keys move too. A browser re-subscribing after a
                    # permission reset gets a **new key pair** for the same
                    # endpoint, and keeping the old one would encrypt every
                    # future message to a key it can no longer read — which
                    # fails silently, as nothing is displayed and the push
                    # service still answers 201.
                    "p256dh": statement.excluded.p256dh,
                    "auth": statement.excluded.auth,
                    "updated_at": statement.excluded.updated_at,
                    "last_seen_at": statement.excluded.last_seen_at,
                    "revoked_at": None,
                },
            ).returning(PushSubscriptionModel)
        )
        return _to_domain(stored.scalar_one())

    async def live_for(self, user_id: uuid.UUID) -> list[PushSubscription]:
        """Every browser this account can currently be reached on.

        One indexed read per notification — `ix_push_subscription__user_live`
        is partial on live rows, so an account with a long history of
        replaced devices costs the same as one with none.
        """
        rows = await self._session.scalars(
            select(PushSubscriptionModel).where(
                PushSubscriptionModel.user_id == user_id,
                PushSubscriptionModel.revoked_at.is_(None),
            )
        )
        return [_to_domain(row) for row in rows]

    async def live_for_many(
        self, user_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, list[PushSubscription]]:
        """The fan-out read: every live subscription for a batch, in one query.

        `IN (...)` over the same partial index `live_for` uses, so a hundred
        recipients cost one round trip rather than a hundred — which matters
        because this runs inside the notification's own transaction.
        """
        if not user_ids:
            return {}

        rows = await self._session.scalars(
            select(PushSubscriptionModel).where(
                PushSubscriptionModel.user_id.in_(set(user_ids)),
                PushSubscriptionModel.revoked_at.is_(None),
            )
        )
        grouped: dict[uuid.UUID, list[PushSubscription]] = defaultdict(list)
        for row in rows:
            grouped[row.user_id].append(_to_domain(row))
        return grouped

    async def get_for(
        self, subscription_id: uuid.UUID, *, user_id: uuid.UUID
    ) -> PushSubscription | None:
        """One subscription, scoped to its owner.

        The owner is half the key rather than a filter applied afterwards —
        there is deliberately no `get(subscription_id)` for a reader to
        reach for.
        """
        row = await self._session.scalar(
            select(PushSubscriptionModel).where(
                PushSubscriptionModel.id == subscription_id,
                PushSubscriptionModel.user_id == user_id,
            )
        )
        return None if row is None else _to_domain(row)

    async def revoke(self, subscription_id: uuid.UUID, *, at: datetime) -> bool:
        """Marks one subscription undeliverable. `True` if it was live.

        **Not owner-scoped, and that is deliberate.** The caller is the
        delivery worker acting on a `410` from a push service, which knows a
        subscription id and has no session. The owner-scoped entry points
        are `get_for` and `revoke_by_endpoint`; this one is reachable only
        from a delivery outcome, never from a request.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(PushSubscriptionModel)
                .where(
                    PushSubscriptionModel.id == subscription_id,
                    PushSubscriptionModel.revoked_at.is_(None),
                )
                .values(revoked_at=at, updated_at=at)
            ),
        )
        return bool(result.rowcount)

    async def revoke_by_endpoint(self, endpoint: str, *, user_id: uuid.UUID, at: datetime) -> bool:
        """Revokes the caller's own subscription for one endpoint.

        Scoped to the owner, so a caller cannot revoke somebody else's
        device by guessing a URL. Returns `False` for an endpoint that is
        not theirs *and* for one that is already revoked — the two are
        deliberately indistinguishable to the caller, because telling them
        apart would answer "does this endpoint belong to another account".
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(PushSubscriptionModel)
                .where(
                    PushSubscriptionModel.endpoint == endpoint,
                    PushSubscriptionModel.user_id == user_id,
                    PushSubscriptionModel.revoked_at.is_(None),
                )
                .values(revoked_at=at, updated_at=at)
            ),
        )
        return bool(result.rowcount)


def _to_domain(row: PushSubscriptionModel) -> PushSubscription:
    return PushSubscription(
        id=row.id,
        user_id=row.user_id,
        endpoint=row.endpoint,
        p256dh=row.p256dh,
        auth=row.auth,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_seen_at=row.last_seen_at,
        revoked_at=row.revoked_at,
    )


__all__ = ["SqlAlchemyPushSubscriptionRepository"]
