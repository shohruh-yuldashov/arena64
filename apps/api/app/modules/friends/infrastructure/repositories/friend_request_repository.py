"""The SQLAlchemy adapter for `application.ports.FriendRequestRepository`.

Database-only (repositories.md §2): this class decides *how* to store and
fetch, never *whether* something may be. Every "is this allowed" question is
answered by `FriendRequestValidator` and the aggregate.

Two responsibilities beyond running SQL, both assigned here by
repositories.md §3:

  **mapping** — between `FriendRequestModel` rows and `FriendRequest`
  aggregates, so nothing above this layer holds an ORM object and inherits
  its lazy-loading and session-lifetime behaviour.

  **error translation** — the partial unique index's violation becomes the
  same `DuplicateFriendRequest` the validator raises. BE-06 requires exactly
  this: the constraint is the authoritative check, and a caller must not be
  able to tell which layer rejected it.
"""

import logging
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Select, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.modules.friends.domain.exceptions import (
    DuplicateFriendRequest,
    FriendRequestAlreadyResolved,
)
from app.modules.friends.domain.friend_request import FriendRequest, FriendRequestStatus
from app.modules.friends.infrastructure.list_cursor import ListCursor
from app.modules.friends.infrastructure.models import FriendRequestModel

logger = logging.getLogger(__name__)

#: The one constraint this adapter translates. Keyed by the name declared in
#: `models.py.__table_args__` — if it is renamed there without being renamed
#: here the translation silently stops working, which is why a contract test
#: drives a real violation through this path rather than trusting the map.
_UNIQUE_PENDING_INDEX = "uq_friend_request__one_pending_per_pair"


class SqlAlchemyFriendRequestRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1) — never holds a session longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: FriendRequestModel) -> FriendRequest:
        """Row to aggregate, field by field.

        Explicit rather than `FriendRequest(**row.__dict__)` for the reason
        `users.application.mappers` gives, plus one specific to an
        aggregate: `__post_init__` re-checks the self-request invariant, so
        a corrupt row fails here rather than reaching a response.
        """
        return FriendRequest(
            id=row.id,
            requester_id=row.requester_id,
            addressee_id=row.addressee_id,
            status=row.status,
            created_at=row.created_at,
            responded_at=row.responded_at,
            expires_at=row.expires_at,
            version=row.version,
        )

    async def add(self, request: FriendRequest) -> FriendRequest:
        row = FriendRequestModel(
            id=request.id,
            requester_id=request.requester_id,
            addressee_id=request.addressee_id,
            status=request.status,
            created_at=request.created_at,
            responded_at=request.responded_at,
            expires_at=request.expires_at,
            version=request.version,
        )
        self._session.add(row)

        try:
            # Flush, never commit — the unit of work owns the transaction
            # (repositories.md §5.1). Flushing here is what turns the
            # constraint violation into an exception *now*, inside this
            # method, where it can be translated.
            await self._session.flush()
        except IntegrityError as error:
            raise self._translate(error) from error

        return self._to_domain(row)

    async def get(self, request_id: UUID) -> FriendRequest | None:
        row = await self._session.get(FriendRequestModel, request_id)
        return self._to_domain(row) if row is not None else None

    async def resolve(self, request: FriendRequest) -> FriendRequest:
        """Writes the transition, guarded by the version it was read at.

        A conditional `UPDATE ... WHERE id = :id AND version = :version`
        rather than a read-modify-write through the identity map, and the
        difference is the whole point: SQLAlchemy would happily flush the
        second device's write over the first, because from its perspective
        both are valid changes to an object it loaded. Matching on the
        version means the database decides, and it decides once.

        `rowcount == 0` is the lost race. It is reported as
        `FriendRequestAlreadyResolved` — the same exception the aggregate
        raises for a request that was already resolved when it was read —
        because a caller cannot act differently on the two and should not
        be able to distinguish them: both mean "your view of this request is
        stale".
        """
        # `cast` because SQLAlchemy types `execute` as returning `Result`,
        # while an `UPDATE` always yields a `CursorResult` — the only kind
        # that carries `rowcount`, which is the whole point of this call.
        statement = (
            update(FriendRequestModel)
            .where(
                FriendRequestModel.id == request.id,
                FriendRequestModel.version == request.version,
            )
            .values(
                status=request.status,
                responded_at=request.responded_at,
                version=request.version + 1,
            )
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))

        if result.rowcount == 0:
            # No `exc_info`: nothing failed. A lost optimistic-concurrency
            # race is an ordinary outcome of two devices, and logging it as
            # an error would make a normal event alertable.
            logger.info(
                "friend_request_resolution_conflict",
                extra={"request_id": str(request.id)},
            )
            raise FriendRequestAlreadyResolved("This friend request has already been resolved.")

        request.version += 1
        return request

    async def find_pending_between(
        self, requester_id: UUID, addressee_id: UUID
    ) -> FriendRequest | None:
        """The live request in **this direction only**.

        Served by `uq_friend_request__one_pending_per_pair` — the partial
        unique index is also the access path, so the two rules it enforces
        cost nothing extra to check.
        """
        row = await self._session.scalar(
            select(FriendRequestModel).where(
                FriendRequestModel.requester_id == requester_id,
                FriendRequestModel.addressee_id == addressee_id,
                FriendRequestModel.status == FriendRequestStatus.PENDING,
            )
        )
        return self._to_domain(row) if row is not None else None

    async def list_for_addressee(
        self,
        addressee_id: UUID,
        *,
        statuses: Sequence[FriendRequestStatus],
        limit: int,
        cursor: str | None,
    ) -> tuple[Sequence[FriendRequest], str | None]:
        return await self._list(
            FriendRequestModel.addressee_id == addressee_id,
            statuses=statuses,
            limit=limit,
            cursor=cursor,
        )

    async def list_for_requester(
        self,
        requester_id: UUID,
        *,
        statuses: Sequence[FriendRequestStatus],
        limit: int,
        cursor: str | None,
    ) -> tuple[Sequence[FriendRequest], str | None]:
        return await self._list(
            FriendRequestModel.requester_id == requester_id,
            statuses=statuses,
            limit=limit,
            cursor=cursor,
        )

    async def _list(
        self,
        party: ColumnElement[bool],
        *,
        statuses: Sequence[FriendRequestStatus],
        limit: int,
        cursor: str | None,
    ) -> tuple[Sequence[FriendRequest], str | None]:
        """Keyset page over `(created_at DESC, id DESC)`.

        **Newest first**, which is what a request list means: the thing you
        have not answered yet is the thing you just received. That makes the
        keyset a *descending* one, so the cursor predicate is `<` rather
        than `>` and the tuple comparison runs the same direction as the
        `ORDER BY` — getting one of those backwards silently returns an
        empty second page, which is why both are written together here
        rather than assembled by a caller.

        `id` is the unique tiebreak. `created_at` alone is not unique — two
        requests can share a millisecond — and a keyset without a unique
        tiebreak skips or repeats rows at a page boundary.

        Over-fetches by one to learn whether a further page exists without a
        second count (RP-03).
        """
        statement: Select[tuple[FriendRequestModel]] = select(FriendRequestModel).where(
            party,
            FriendRequestModel.status.in_(statuses),
        )

        if cursor is not None:
            position = ListCursor.decode(cursor)
            statement = statement.where(
                or_(
                    FriendRequestModel.created_at < position.created_at,
                    and_(
                        FriendRequestModel.created_at == position.created_at,
                        FriendRequestModel.id < position.row_id,
                    ),
                )
            )

        statement = statement.order_by(
            FriendRequestModel.created_at.desc(), FriendRequestModel.id.desc()
        ).limit(limit + 1)

        rows = list((await self._session.scalars(statement)).all())

        has_more = len(rows) > limit
        page = rows[:limit]

        next_cursor: str | None = None
        if has_more and page:
            last = page[-1]
            next_cursor = ListCursor(created_at=last.created_at, row_id=last.id).encode()

        return [self._to_domain(row) for row in page], next_cursor

    @staticmethod
    def _translate(error: IntegrityError) -> Exception:
        """A driver `IntegrityError` as the typed exception the validator
        would have raised.

        Only the partial unique index is translated. The two `CHECK`s are
        *unreachable* from the application — the aggregate refuses a
        self-request before construction and sets `responded_at` in the same
        statement as `status` — so a violation of either means a row was
        written by something other than this code path, and the honest
        outcome is the generic 500 an untranslated `IntegrityError` becomes.
        Mapping them would claim a client error for what is a data-integrity
        incident.
        """
        constraint = getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)
        if constraint == _UNIQUE_PENDING_INDEX:
            return DuplicateFriendRequest("You already have a pending request to that player.")
        return error
