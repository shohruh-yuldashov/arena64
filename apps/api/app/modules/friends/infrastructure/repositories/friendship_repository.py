"""The SQLAlchemy adapter for `application.ports.FriendshipRepository`.

Database-only (repositories.md §2): decides *how* to store and fetch, never
*whether*. Every "is this allowed" question is answered by `Friendship` and
`FriendshipService`.

## Every query reduces the pair to canonical order first

DB-12 stores one row per unordered pair, so a caller asking "are A and B
friends" must sort before it can touch the index. That sorting happens in
`canonical_pair`, which is the domain's, and never inline here — two
definitions of the ordering would be the failure DB-12 describes: the
invariant holding everywhere except the one path that sorted differently.

## Reading "friendships of player X" without knowing which side X is on

An `OR` across both columns, served by the two partial indexes §12.3
specifies. PostgreSQL `BitmapOr`s them and sorts the result, which is the
cost DB-12 chose to pay for storing the relationship once — "index entries
are cheap and derived; rows are facts that can disagree."

The sort is bounded by how many friends a player has, not by the size of
the table, so it is a real cost and a small one. If a player with tens of
thousands of friends ever exists, the shape that removes the sort is a
`UNION ALL` of two index-ordered legs merged by the keyset — recorded here
so the next person does not have to derive it, and deliberately not built
for a bound nobody has approached.
"""

import logging
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Select, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.friends.domain.exceptions import FriendshipAlreadyEnded
from app.modules.friends.domain.friendship import Friendship, canonical_pair
from app.modules.friends.infrastructure.list_cursor import ListCursor
from app.modules.friends.infrastructure.models import FriendshipModel

logger = logging.getLogger(__name__)

#: The one constraint this adapter translates. Keyed by the name declared in
#: `models.py.__table_args__` — renaming it there without renaming it here
#: silently stops the translation working, which is why a contract test
#: drives a real violation through this path.
_UNIQUE_LIVE_PAIR_INDEX = "uq_friendship__pair"


class SqlAlchemyFriendshipRepository:
    """Constructed per use case with the active unit of work's session
    (repositories.md §5.1) — never holds a session longer than that."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: FriendshipModel) -> Friendship:
        """Row to aggregate, field by field.

        Explicit rather than `Friendship(**row.__dict__)` for the reason
        `users.application.mappers` gives, plus one specific to this
        aggregate: `__post_init__` re-checks the canonical ordering, so a
        row written out of order by a repair script fails here rather than
        rendering somebody as a friend of themselves.
        """
        return Friendship(
            id=row.id,
            player_low_id=row.player_low_id,
            player_high_id=row.player_high_id,
            created_at=row.created_at,
            source_request_id=row.source_request_id,
            ended_at=row.ended_at,
            ended_reason=row.ended_reason,
        )

    @staticmethod
    def _live(statement: Select[Any]) -> Select[Any]:
        """Restricts to live friendships.

        One definition, applied by `exists`, `friends_of` and
        `friend_count`, so the three cannot drift into disagreeing about
        what "a friend" is — which would show as a count that does not match
        the list beside it.
        """
        return statement.where(FriendshipModel.ended_at.is_(None))

    async def create(self, friendship: Friendship) -> Friendship:
        """Persists a new friendship.

        Raises `FriendshipAlreadyExists` when the partial unique index
        refuses a second live row for the pair. The service checks the same
        rule first to produce a good error cheaply; this is the guard that
        holds under concurrency, and BE-06 makes it the authoritative one —
        two acceptances racing both pass a check-then-act.
        """
        row = FriendshipModel(
            id=friendship.id,
            player_low_id=friendship.player_low_id,
            player_high_id=friendship.player_high_id,
            created_at=friendship.created_at,
            source_request_id=friendship.source_request_id,
            ended_at=friendship.ended_at,
            ended_reason=friendship.ended_reason,
        )
        self._session.add(row)

        try:
            # Flush, never commit — the unit of work owns the transaction
            # (repositories.md §5.1). On the acceptance path that unit of
            # work is the *request's*, which is what makes FR-4 hold: the
            # resolved request and this row commit together or not at all.
            await self._session.flush()
        except IntegrityError as error:
            raise self._translate(error) from error

        return self._to_domain(row)

    async def exists(self, player_a: UUID, player_b: UUID) -> bool:
        """Whether the two players are currently friends.

        Takes the pair **unordered** — no caller should have to know about
        `low` and `high` — and sorts it here through the domain's
        `canonical_pair`.
        """
        low, high = canonical_pair(player_a, player_b)
        found = await self._session.scalar(
            self._live(
                select(FriendshipModel.id).where(
                    FriendshipModel.player_low_id == low,
                    FriendshipModel.player_high_id == high,
                )
            ).limit(1)
        )
        return found is not None

    async def find_between(self, player_a: UUID, player_b: UUID) -> Friendship | None:
        """The live friendship between the two, or `None`.

        Separate from `exists` because removal needs the aggregate — it has
        to check participation and record an end reason — while the
        relationship provider needs only a yes or no, and answering that
        with a full row read on every profile render would be work spent to
        discard it.
        """
        low, high = canonical_pair(player_a, player_b)
        row = await self._session.scalar(
            self._live(
                select(FriendshipModel).where(
                    FriendshipModel.player_low_id == low,
                    FriendshipModel.player_high_id == high,
                )
            )
        )
        return self._to_domain(row) if row is not None else None

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        """Which of `others` are currently friends with `player_id`, in one
        query.

        The batch read behind `ViewerRelationship.FRIEND`. A search page or
        a request list renders up to fifty players, and asking `exists`
        once per row would be the N+1 pattern CLAUDE.md §10.4 names — on the
        composition path, where it would multiply every profile render.

        Returns the *other* players' ids rather than friendship rows,
        because that is what a caller does with the answer: index it.
        """
        if not others:
            return set()

        candidates = set(others)
        rows = await self._session.execute(
            self._live(
                select(FriendshipModel.player_low_id, FriendshipModel.player_high_id).where(
                    or_(
                        and_(
                            FriendshipModel.player_low_id == player_id,
                            FriendshipModel.player_high_id.in_(candidates),
                        ),
                        and_(
                            FriendshipModel.player_high_id == player_id,
                            FriendshipModel.player_low_id.in_(candidates),
                        ),
                    )
                )
            )
        )

        # Both columns are selected and the viewer's own id filtered out,
        # rather than selecting "the other one" with a CASE: the branch is
        # simpler to read here than in SQL, and PostgreSQL is returning two
        # UUIDs either way.
        return {high if low == player_id else low for low, high in rows if player_id in (low, high)}

    async def friend_count(self, player_id: UUID) -> int:
        """How many live friendships this player has.

        A `COUNT` rather than the length of a page, so it is correct beyond
        the first page and costs one index-only scan of the two partial
        indexes rather than materialising every row.

        Deliberately **not** cached. `friends:v1:` is reserved for exactly
        this and A64-013.3 excludes Redis — a count with no invalidation
        trigger is a number that goes wrong on the first removal, and
        caching.md C-1 requires the trigger before the first key.
        """
        total = await self._session.scalar(
            self._live(
                select(func.count()).select_from(FriendshipModel).where(_involves(player_id))
            )
        )
        return int(total or 0)

    async def friends_of(
        self, player_id: UUID, *, limit: int, cursor: str | None
    ) -> tuple[Sequence[Friendship], str | None]:
        """This player's live friendships, newest first, keyset-paginated.

        Ordered by `(created_at DESC, id DESC)` — the most recently formed
        friendship first, which is what a list of people you have just added
        should show. `id` is the unique tiebreak a keyset needs: two
        friendships can share a millisecond, and without it a page boundary
        would skip or repeat rows.

        Over-fetches by one to learn whether a further page exists without a
        second count (RP-03).
        """
        statement: Select[tuple[FriendshipModel]] = self._live(
            select(FriendshipModel).where(_involves(player_id))
        )

        if cursor is not None:
            position = ListCursor.decode(cursor)
            # `<` rather than `>`, matching the descending order. Getting
            # one of the two backwards silently returns an empty second
            # page, which is why both are written here rather than assembled
            # by a caller.
            statement = statement.where(
                or_(
                    FriendshipModel.created_at < position.created_at,
                    and_(
                        FriendshipModel.created_at == position.created_at,
                        FriendshipModel.id < position.row_id,
                    ),
                )
            )

        statement = statement.order_by(
            FriendshipModel.created_at.desc(), FriendshipModel.id.desc()
        ).limit(limit + 1)

        rows = list((await self._session.scalars(statement)).all())

        has_more = len(rows) > limit
        page = rows[:limit]

        next_cursor: str | None = None
        if has_more and page:
            last = page[-1]
            next_cursor = ListCursor(created_at=last.created_at, row_id=last.id).encode()

        return [self._to_domain(row) for row in page], next_cursor

    async def remove(self, friendship: Friendship) -> Friendship:
        """Records the end of a friendship, guarded by its live state.

        A conditional `UPDATE ... WHERE id = :id AND ended_at IS NULL`
        rather than a read-modify-write, for the reason
        `SqlAlchemyFriendRequestRepository.resolve` gives about versions:
        two devices removing at once would otherwise both succeed, and the
        second would overwrite the first's `ended_at` with a later instant.

        `rowcount == 0` means the row ended between the read and this write.
        Reported as `FriendshipAlreadyEnded` — the same exception the
        aggregate raises for a friendship that was already ended when it was
        read, because a caller cannot act differently on the two.

        **Never a `DELETE`.** database.md §1221: a friendship that ended is
        a fact with a date. It is also what lets the pair be friends again,
        since the unique index covers only live rows.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(FriendshipModel)
                .where(
                    FriendshipModel.id == friendship.id,
                    FriendshipModel.ended_at.is_(None),
                )
                .values(ended_at=friendship.ended_at, ended_reason=friendship.ended_reason)
            ),
        )

        if result.rowcount == 0:
            # No `exc_info`: nothing failed. Two devices removing the same
            # friendship is an ordinary outcome, and logging it as an error
            # would make a normal event alertable.
            logger.info("friendship_removal_conflict", extra={"friendship_id": str(friendship.id)})
            raise FriendshipAlreadyEnded("This friendship has already ended.")

        return friendship

    @staticmethod
    def _translate(error: IntegrityError) -> Exception:
        """A driver `IntegrityError` as the typed exception the service
        would have raised.

        Only the partial unique index is translated. `ck_friendship__canonical_order`
        and the `ended_*` pairing CHECK are unreachable from the application
        — `Friendship.between` sorts and `end` writes both fields together —
        so a violation of either means a row was written by something other
        than this code path, and the honest outcome is the generic 500 an
        untranslated `IntegrityError` becomes. Mapping them would claim a
        client error for a data-integrity incident.
        """
        from app.modules.friends.domain.exceptions import FriendshipAlreadyExists

        constraint = getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)
        if constraint == _UNIQUE_LIVE_PAIR_INDEX:
            return FriendshipAlreadyExists("These players are already friends.")
        return error


def _involves(player_id: UUID) -> Any:
    """`player_id` is on either side of the pair.

    One definition, used by the count and the list, so the two cannot
    disagree about which rows belong to a player — a count that did not
    match the list beside it is the kind of defect nobody reports as a bug
    because it looks like a caching artefact.

    Served by the two partial indexes §12.3 specifies; PostgreSQL
    `BitmapOr`s them.
    """
    return or_(
        FriendshipModel.player_low_id == player_id,
        FriendshipModel.player_high_id == player_id,
    )
