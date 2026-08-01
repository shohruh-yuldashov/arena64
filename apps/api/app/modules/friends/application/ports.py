"""The ports `friends` programs against — AD-06: declared in
`application/`, satisfied by `infrastructure/`.

Two ports since A64-013.3. This module owns its own storage, so unlike
`profiles` there is no cross-context read here and nothing to adapt — these
are the whole surface between the use cases and PostgreSQL.

There is still deliberately no `BlockRepository`. A64-013.3 does not
implement blocking, and a port with no implementation is a hole that reads
as supported (`statistics.application.ports` makes the same argument about a
writer that does not exist yet).

## Why two ports rather than one `FriendsRepository`

They are two aggregates with two lifecycles and two relations, and the
capability each grants is different: `FriendRequestService` needs both,
while the *relationship provider* that answers "are these two friends" on
every profile render needs only `FriendshipRepository.friend_ids_among` and
must not be able to resolve a request. Merging them would hand the
composition path the ability to accept requests.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.modules.friends.domain.friend_request import FriendRequest, FriendRequestStatus
from app.modules.friends.domain.friendship import Friendship, FriendshipMetadata


class FriendRequestRepository(Protocol):
    """Collection-like access to the `FriendRequest` aggregate.

    A `Protocol`, not an ABC, so the SQLAlchemy adapter and an in-memory
    fake satisfy it structurally without inheriting from anything this
    module owns (repositories.md RP-05).

    **Decides how to store and fetch, never whether.** Every "is this
    allowed" question — may this player send, may this player accept — is
    answered by `FriendRequestValidator` and the aggregate. The one
    exception is the uniqueness rule, which is a *database* constraint by
    necessity (BE-06: two concurrent sends both pass a check-then-act, and
    only the partial unique index is correct under concurrency), so this
    port translates that violation into the same exceptions the validator
    raises.
    """

    async def add(self, request: FriendRequest) -> FriendRequest:
        """Persists a new request.

        Raises `DuplicateFriendRequest` when the partial unique index
        refuses a second pending request for the ordered pair (FR-1). The
        validator checks the same rule first; this is the guard that holds
        under concurrency, and a caller cannot tell which layer rejected it.
        """
        ...

    async def get(self, request_id: UUID) -> FriendRequest | None:
        """One request by identifier, or `None`.

        `None` rather than raising, because the caller has more context: the
        service turns it into `FriendRequestNotFound` after deciding whether
        the caller was even entitled to ask.
        """
        ...

    async def resolve(self, request: FriendRequest) -> FriendRequest:
        """Writes a status transition, guarded by the version it was read
        at.

        **Optimistic concurrency** (repositories.md §8.4). The `UPDATE`
        matches on `(id, version)` and bumps the version, so two devices
        resolving the same request concurrently produce one winner and one
        `FriendRequestAlreadyResolved` — rather than two successful writes
        whose final state is whichever landed second.

        Raises `FriendRequestAlreadyResolved` when no row matched, which is
        the same exception the aggregate raises for a request that was
        already resolved *before* it was read. A caller cannot distinguish
        the two, and should not: both mean "your view of this request is
        stale".
        """
        ...

    async def find_pending_between(
        self, requester_id: UUID, addressee_id: UUID
    ) -> FriendRequest | None:
        """The live request from `requester_id` to `addressee_id`, if any.

        **Directional.** A request from A to B and one from B to A are
        different facts, so the validator calls this twice — once each way —
        rather than this port taking an unordered pair. That keeps the two
        rules distinguishable: a duplicate and an opposite-direction request
        are different conflicts with different codes and different advice
        for the client.
        """
        ...

    async def list_for_addressee(
        self,
        addressee_id: UUID,
        *,
        statuses: Sequence[FriendRequestStatus],
        limit: int,
        cursor: str | None,
    ) -> tuple[Sequence[FriendRequest], str | None]:
        """Requests *received* by this player, newest first, keyset-paginated.

        Returns the page and the cursor for the next one, or `None` when
        this is the last. Ordered by `(created_at DESC, id DESC)` — the
        ordering key database.md's keyset guidance names, with `id` as the
        unique tiebreak a keyset needs to avoid skipping rows at a page
        boundary.

        `statuses` is a filter rather than a fixed `PENDING`, because
        A64-013.3's friend list and a future request history both read this
        relation with a different set, and a method that hard-coded the
        status would be copied rather than reused.

        Raises `InvalidFriendRequestCursor` for a malformed cursor.
        """
        ...

    async def list_for_requester(
        self,
        requester_id: UUID,
        *,
        statuses: Sequence[FriendRequestStatus],
        limit: int,
        cursor: str | None,
    ) -> tuple[Sequence[FriendRequest], str | None]:
        """Requests *sent* by this player. See `list_for_addressee`.

        Two methods rather than one taking a direction flag, which
        CLAUDE.md §2.3 rules out: a boolean parameter that selects
        behaviour is two functions wearing one name, and here the two
        genuinely differ — they filter different columns and serve
        different endpoints.
        """
        ...


class FriendshipRepository(Protocol):
    """Collection-like access to the `Friendship` aggregate — A64-013.3.

    **Every method takes the pair unordered.** DB-12 stores one row per
    unordered pair in canonical order, and no caller should have to know
    about `low` and `high` — the adapter sorts through the domain's
    `canonical_pair`, which is the single definition of the ordering.

    Read-only for everything except `create` and `remove`, and there is no
    method that could rewrite a friendship's participants. A friendship is
    formed and it ends; it never moves.
    """

    async def create(self, friendship: Friendship) -> Friendship:
        """Persists a new friendship.

        Raises `FriendshipAlreadyExists` when the partial unique index
        refuses a second live row for the pair. The service checks first to
        produce a good error cheaply; this is the guard that holds under
        concurrency (BE-06).

        **Flushes, never commits.** On the acceptance path the caller's unit
        of work is the *request's*, which is what makes FR-4 hold: the
        resolved request and this row commit together or not at all.
        """
        ...

    async def exists(self, player_a: UUID, player_b: UUID) -> bool:
        """Whether the two are currently friends. Order-independent."""
        ...

    async def friendship_by_players(self, player_a: UUID, player_b: UUID) -> Friendship | None:
        """The live friendship between the two, or `None`.

        Separate from `exists` because removal needs the aggregate — it
        checks participation and records an end reason — while the
        relationship provider needs only a yes or no, and answering that
        with a full row read on every profile render would be work spent to
        discard it.

        Named for the pair rather than for the lookup (`find_between` until
        A64-013.4), because there are now three ways to reach a friendship
        and the argument is what distinguishes them.
        """
        ...

    async def mutual_friend_count(self, player_a: UUID, player_b: UUID) -> int:
        """How many live friends the two players have in common —
        A64-013.4.

        **The only definition of "mutual friend" on the platform**, and it
        lives here rather than in a service by requirement: a service
        intersecting two friend lists in Python would be a second definition
        that disagrees the first time one of them forgets `ended_at`.

        Returns `0` for two players who are not friends — a mutual-friend
        count is a fact about two friend lists and is well defined for
        strangers. Whether it may be *shown* is the caller's question.

        Order-independent, like every method here.
        """
        ...

    async def friendship_metadata(
        self, viewer_id: UUID, other_id: UUID
    ) -> FriendshipMetadata | None:
        """The read model for one relationship, or `None` for strangers.

        Returns `FriendshipMetadata` rather than the aggregate: a caller
        inspecting a relationship must not be handed something it could
        transition, and the derived counts a reader wants are not the
        aggregate's to compute.

        `None` covers "never were friends" and "the friendship ended"
        indistinguishably, which is what an inspection endpoint needs —
        whether two people were *ever* friends is not a question it should
        answer.

        **Designed so a cache can be introduced without changing this
        signature.** `friends:v1:` remains unwritten (caching.md C-1 wants
        the invalidation trigger first); when it arrives it wraps this
        method, whose arguments and return type already carry everything a
        key and a value would need.
        """
        ...

    async def friend_ids_among(self, player_id: UUID, others: Sequence[UUID]) -> set[UUID]:
        """Which of `others` are currently friends with `player_id`, in one
        query.

        The batch read behind `ViewerRelationship.FRIEND`. A page renders up
        to fifty players, and asking `exists` per row would put the N+1
        pattern CLAUDE.md §10.4 names on the composition path — multiplying
        every profile render on the platform.

        An empty `others` returns an empty set without touching the
        database.
        """
        ...

    async def friend_count(self, player_id: UUID) -> int:
        """How many live friendships this player has.

        A real count rather than the length of a page, so it is correct
        beyond the first one.
        """
        ...

    async def friends_of(
        self, player_id: UUID, *, limit: int, cursor: str | None
    ) -> tuple[Sequence[Friendship], str | None]:
        """This player's live friendships, newest first, keyset-paginated.

        Returns the page and the cursor for the next one, or `None` when
        this is the last. Raises `InvalidFriendsCursor` for a malformed
        cursor.
        """
        ...

    async def remove(self, friendship: Friendship) -> Friendship:
        """Records the end of a friendship.

        Never deletes — database.md §1221: a friendship that ended is a fact
        with a date. Raises `FriendshipAlreadyEnded` if it ended between the
        read and this write, which is two devices removing at once.
        """
        ...
