"""`FriendshipService` — list friends, count them, inspect one, end one.

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
from app.modules.friends.domain.friendship import (
    Friendship,
    FriendshipEndReason,
    FriendshipMetadata,
)

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

    async def remove_friend(self, *, player_id: UUID, other_id: UUID) -> None:
        """Ends the friendship between `player_id` and `other_id`.

        **Unilateral and silent** (FS-2). Either party may do this, neither
        needs the other's agreement, and nothing notifies the other party —
        "requiring mutual agreement to stop being friends is not a feature
        anyone wants."

        ## Idempotent — A64-013.4

        Removing somebody you are not friends with **succeeds and does
        nothing**. It does not raise, and this is a deliberate change from
        A64-013.3, which answered `404`.

        Two reasons, and the second is the stronger:

          - `DELETE` is idempotent by HTTP semantics. A client retrying
            after a dropped response must not be told the resource is gone
            when its own first attempt is what removed it, and a UI that
            double-fires a button should not surface an error for an
            outcome the user got.
          - **It stops the endpoint answering a question it should not.** A
            `404` for "you are not friends" and a `204` for "you were" is an
            oracle: anybody holding a player id could probe their own
            relationship state, and — once A64-013.5 voids friendships on a
            block — could detect having been blocked by watching a removal
            turn into a 404. One answer for both cases closes that.

        Ownership needs no check here because there is nothing to check:
        `player_id` comes from the access token and the lookup is by pair,
        so the only friendship reachable is one the caller is in. A third
        party asking to remove two other people finds no friendship *of
        theirs* and gets the same silent success — having changed nothing.

        `Friendship.end` still refuses a non-participant, and still runs:
        the check belongs in the aggregate, where it protects a caller this
        service has not met yet.

        Returns `None`. A64-013.3 returned the ended `Friendship`, which
        made sense when the call always produced one; now that it may
        produce nothing, returning "the friendship you ended, or None"
        would push the idempotency the caller was spared straight back at
        it.
        """
        friendship = await self._friendships.friendship_by_players(player_id, other_id)

        if friendship is None:
            # Nothing to end. Logged at DEBUG rather than INFO: a retry is
            # an ordinary event and an audit trail records what *changed*,
            # so a line per no-op would dilute the removals that did.
            logger.debug("friendship_removal_noop", extra={"actor_id": str(player_id)})
            return

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

    async def friendship_details(self, *, player_id: UUID, other_id: UUID) -> FriendshipMetadata:
        """What `player_id` may learn about their friendship with
        `other_id` — A64-013.4.

        Raises `FriendshipNotFound` (404) when the two are not currently
        friends, covering "never were" and "it ended" indistinguishably.
        That is the opposite decision from `remove_friend` above, and the
        asymmetry is deliberate: a `DELETE` must be idempotent and therefore
        cannot signal absence, while a `GET` for a resource that does not
        exist has no other honest answer than `404`.

        Neither leaks more than the other. The removal's silence and this
        `404` both stop at "you and this player are not friends", which the
        caller is a party to and already knows; what neither reveals is
        anything about the *other* player's relationships.

        **Ownership is structural.** `player_id` comes from the token and
        the lookup is by pair, so the only relationship inspectable is one
        the caller is in — there is no arrangement of parameters that could
        ask about two other people.
        """
        metadata = await self._friendships.friendship_metadata(player_id, other_id)
        if metadata is None:
            raise FriendshipNotFound("You are not friends with that player.")

        # DEBUG, not INFO. Inspecting a relationship changes nothing, and an
        # audit log records changes; at INFO this would fire on every render
        # of a friend's profile card and drown the removals beside it
        # (services.md §7.1). The actor only — who they looked at is the
        # social-graph edge this must not accumulate.
        logger.debug("friendship_inspected", extra={"actor_id": str(player_id)})

        return metadata

    async def mutual_friend_count(self, *, player_id: UUID, other_id: UUID) -> int:
        """How many friends the two have in common.

        Delegates entirely: A64-013.4 requires the calculation to exist once
        and in the repository, so there is nothing for a service to add —
        and computing it here from two friend lists would be the second
        definition that requirement exists to prevent.

        **Reachable from no endpoint.** A64-013.4 scopes mutual counts to
        "repository/service only", so this is the seam a later UX surface
        calls rather than a published capability. It works, it is tested,
        and nothing on the wire carries its result.

        Well defined for two players who are not friends — a count of shared
        friends is a fact about two lists — so this does not raise for
        strangers. Whether the number may be *shown* to a particular caller
        is a question for whoever eventually publishes it.
        """
        return await self._friendships.mutual_friend_count(player_id, other_id)


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
