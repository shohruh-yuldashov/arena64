"""`FriendshipService` — list friends, count them, end one.

Orchestrates; does not compute (services.md §3.2). The canonical ordering is
the domain's, participation is the aggregate's, uniqueness is the partial
index's, and what lives here is the sequencing, the transaction boundary and
the audit log.

## Creating a friendship is *not* one of these use cases

There is no `create` method taking two player ids, and its absence is the
design. A64-013.3: "friendship created only after accepted request."

The only way a friendship comes into existence is
`FriendRequestService.accept`, which builds the aggregate and persists it
**inside the request's own unit of work** (FR-4). A `FriendshipService.create`
would be a second door into the same relation, reachable without a request
and — worse — with its own transaction, which is exactly the split
A64-013.3 forbids: "never create accepted request → second transaction →
friendship."

So this service reads and ends. It never begins.

## Ownership is participation, and it is checked in the aggregate

`Friendship.end` calls `require_participant` before it transitions, so a
caller who is not one of the two cannot end the relationship however they
reached this service. That is the arrangement `FriendRequestService`
documents at length: a check in a service protects that service's callers,
while a check in the aggregate protects every future one.

The friend *list* needs no such check for a different reason — it is scoped
to the token's own account by construction, and there is no parameter that
could name another player's list.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from app.core.clock import Clock
from app.core.unit_of_work import UnitOfWork
from app.modules.friends.application.ports import FriendshipRepository
from app.modules.friends.domain.exceptions import FriendshipNotFound
from app.modules.friends.domain.friendship import Friendship, FriendshipEndReason

logger = logging.getLogger(__name__)


class FriendshipService:
    def __init__(
        self,
        *,
        friendships: FriendshipRepository,
        unit_of_work: UnitOfWork,
        clock: Clock,
    ) -> None:
        self._friendships = friendships
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def list_friends(
        self, *, player_id: UUID, limit: int, cursor: str | None
    ) -> tuple[Sequence[Friendship], str | None]:
        """This player's live friendships, newest first.

        Read-only; opens no transaction. Returns the aggregates rather than
        composed profiles — turning friendships into people is the
        presentation layer's job, through `ProfileDirectoryService`, which is
        what keeps this service free of any dependency on `profiles`.
        """
        return await self._friendships.friends_of(player_id, limit=limit, cursor=cursor)

    async def count_friends(self, *, player_id: UUID) -> int:
        """How many friends this player has. Read-only.

        Deliberately **uncached**. `friends:v1:` is reserved for exactly
        this and A64-013.3 excludes Redis: a count with no invalidation
        trigger goes wrong on the first removal, and caching.md C-1 requires
        the trigger to be written down before the first key. The extension
        point is this method — one place to wrap, once the trigger exists.
        """
        return await self._friendships.friend_count(player_id)

    async def remove_friend(self, *, player_id: UUID, other_id: UUID) -> Friendship:
        """Ends the friendship between `player_id` and `other_id`.

        **Unilateral and silent** (FS-2). Either party may do this, neither
        needs the other's agreement, and nothing notifies the other party —
        "requiring mutual agreement to stop being friends is not a feature
        anyone wants."

        Raises `FriendshipNotFound` (404) when the two are not currently
        friends, which deliberately covers both "never were" and "already
        ended". The distinction is not a caller's to learn: whether two
        people are friends is what `VisibilityLevel.FRIENDS` exists to
        control, and an endpoint answering it differently for the two cases
        would be a way to ask.

        `NotFriendshipParticipant` is unreachable from the HTTP path —
        `player_id` comes from the token and the lookup is by pair, so the
        caller is always one of the two — and the check runs anyway, in the
        aggregate, because that is where it protects a caller this service
        has not met yet.
        """
        friendship = await self._friendships.find_between(player_id, other_id)
        if friendship is None:
            raise FriendshipNotFound("You are not friends with that player.")

        friendship.end(
            by=player_id,
            at=self._clock.now(),
            reason=FriendshipEndReason.REMOVED,
        )

        async with self._unit_of_work:
            removed = await self._friendships.remove(friendship)
            await self._unit_of_work.commit()

        # **The actor and the friendship, never the other party.** Who
        # removed whom is a social-graph edge, and FS-2's silence is about
        # not telling the other person — a log line naming both would put
        # the fact somewhere with broader read access than the row it came
        # from (services.md §8.5). The friendship id resolves to both for
        # anyone entitled to look.
        logger.info(
            "friendship_removed",
            extra={"friendship_id": str(removed.id), "actor_id": str(player_id)},
        )
        return removed


class FriendshipReaderService:
    """The implementation behind `friends.public.ports.FriendshipReader`.

    The same shape as `users`' published-port adapters: a thin translation
    with no rule of its own. It exists so that `profiles` depends on a
    published *port* rather than on this module's repository (R-1), and so
    that the capability crossing the boundary is exactly one read.

    Deliberately **not** a method on `FriendshipService` above. That service
    can end friendships; this adapter cannot, and a consumer granted one
    should not thereby hold the other — the narrowing `users.public` makes
    twelve times over.
    """

    def __init__(self, friendships: FriendshipRepository) -> None:
        self._friendships = friendships

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        """Which of `others` are currently friends with `player_id`.

        Straight delegation. The seam is the point rather than the code:
        `profiles` sees a port it can only read through, and this module
        keeps the freedom to change how the answer is computed — a cache
        under `friends:v1:` is the obvious next form, and it lands here
        without `profiles` learning that it happened.
        """
        return await self._friendships.friend_ids_among(player_id, others)
