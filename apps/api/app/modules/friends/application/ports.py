"""The ports `friends` programs against — AD-06: declared in
`application/`, satisfied by `infrastructure/`.

Three ports since A64-013.5. This module owns its own storage, so unlike
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
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.friends.domain.block import Block
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

    async def void_pending_between(self, player_a: UUID, player_b: UUID, *, at: datetime) -> int:
        """Resolves every pending request **between** the two, in either
        direction, to `VOIDED`. Returns how many were resolved.

        Added by A64-013.5 for the blocking cascade (FR-2, BL-2). Both
        directions in one statement, because a block suppresses contact
        symmetrically and leaving the reverse request pending would let the
        blocked player's request sit in the blocker's inbox.

        A set-based `UPDATE` rather than reading the aggregates and
        resolving each: there are at most two rows (FR-1 permits one pending
        request per ordered pair), but the write must be one statement so it
        cannot half-apply, and there is no per-row decision to make — the
        transition is the same for every match and the actor is not a party
        to it.

        **Deliberately bypasses the aggregate's version check**, which is
        the one place on this platform that does. A block is not a race
        between two devices resolving one request; it is a unilateral act
        that must win, and losing to a concurrent accept would leave a
        friendship formed against a block placed first.

        Returns the count so the caller can log what the cascade actually
        did — `0` is the common case and is not a failure.
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

        Still here after A64-013.6 wrapped this repository in a cache, and
        deliberately: the *cached* reader answers from the whole friend set,
        but a cache miss falls through to this, and the narrower query is
        cheaper when the page is small. Both paths exist because both are
        the right answer in different states.
        """
        ...

    async def friend_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """**Every** live friend of this player — A64-013.6.

        The value `friends:v1:friends:<player_id>` caches. It is the whole
        set rather than a filtered one because a cache keyed on a query
        would need a key per distinct page: unbounded keys, the same four
        invalidation triggers, and a hit rate near zero.

        Bounded by the player's friend count, which is bounded by nothing
        today — the same open question BL-4 raises about blocks. Recorded
        here rather than guessed at, because the fix is a product decision
        and not an engineering one.

        A `frozenset` because every consumer only tests membership or
        intersects, and because the cache stores it as one.
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


class BlockedPlayerRepository(Protocol):
    """Collection-like access to the `Block` aggregate — A64-013.5.

    **Directional throughout, except `blocked_ids_for`.** A block is a
    one-directional fact (BL-1), so `add`, `exists`, `remove` and
    `list_for_blocker` all name a blocker and a blocked player in that
    order. The one symmetric method is the one every *consumer* needs,
    because the visibility consequence of a block runs both ways even though
    the fact does not.

    No update of any kind. A block is created and deleted; nothing about it
    is ever modified, which is why `Block` is frozen.
    """

    async def add(self, block: Block) -> Block:
        """Persists a new block.

        Raises `AlreadyBlocked` when the unique index refuses a duplicate —
        the guard that holds under concurrency, and one that matters more
        than usual here because a second block would run the cascade twice.

        Flushes, never commits: the caller's unit of work spans the block,
        the friendship it ends and the requests it voids.
        """
        ...

    async def exists(self, blocker_id: UUID, blocked_id: UUID) -> bool:
        """Whether `blocker_id` has blocked `blocked_id`. Directional."""
        ...

    async def blocked_ids_for(self, player_id: UUID) -> frozenset[UUID]:
        """Every player this one cannot interact with, in **either**
        direction.

        The set behind `ViewerRelationship.BLOCKED`, the search exclusion
        and the friend-request refusal. One query, one union, so the three
        consumers cannot disagree about which directions count — the one
        that forgot a direction would leak in exactly the way BL-1 forbids.

        **Designed so a cache can be introduced without changing this
        signature.** `friends:v1:` remains unwritten (caching.md C-1 wants
        the invalidation trigger first, and here it is `block` and
        `unblock` — both in `BlockingService`).
        """
        ...

    async def list_for_blocker(
        self, blocker_id: UUID, *, limit: int, cursor: str | None
    ) -> tuple[Sequence[Block], str | None]:
        """The blocks this player has **placed**, newest first,
        keyset-paginated.

        One-directional on purpose: blocks placed *on* you are not yours to
        see, which is the whole reason a block is worth placing (BL-1).
        """
        ...

    async def remove(self, blocker_id: UUID, blocked_id: UUID) -> None:
        """Lifts a block — a **hard delete** (database.md §7.2).

        Raises `NotBlocked` when there was none. `BlockingService.unblock`
        catches it, because unblocking is idempotent; the repository reports
        what happened rather than deciding what it means.
        """
        ...


class SocialGraphEntry(StrEnum):
    """Which cached set is being asked for — A64-013.8.

    Replaces the string key the port used to take. The old shape had the
    *application* layer building `friends:v1:blocked:<id>` through a helper
    in `infrastructure.cache.keys`, which is a dependency pointing the wrong
    way (CLAUDE.md §3.1) — and it was invisible until an import contract went
    looking for it.

    An enum keeps the split honest: the **application** names the entry, the
    **adapter** knows the keyspace. Adding a third entry is a member here and
    a branch in one adapter, and caching.md C-2's version segment stays in
    exactly one file.
    """

    FRIENDS = "friends"
    """Every live friend of a player."""

    BLOCKED = "blocked"
    """Every player this one cannot interact with, in either direction."""


class SocialGraphCache(Protocol):
    """Where the social graph is cached, and how it is dropped —
    A64-013.6.

    Keyed by **player and entry**, not by a string: the keyspace belongs to
    infrastructure (`friends.infrastructure.cache.keys`), and a port that
    took a pre-built key would put half the key layout in `application/` and
    the other half in `infrastructure/`.

    Invalidation takes player ids because *that* is the vocabulary of the
    four triggers: a request was accepted between two players, a block was
    lifted on one. Which keys those touch is the adapter's business, and
    `keys_for` is what makes a third entry invalidate automatically.

    **Nothing here raises.** A cache that failed loudly would convert an
    optimisation into a dependency, and every method's contract says so.
    """

    async def get_ids(self, player_id: UUID, entry: SocialGraphEntry) -> frozenset[UUID] | None:
        """The cached id set, or `None` on a miss.

        A miss covers an absent key, an unreachable cache, a slow one and a
        malformed value — all of which mean the same thing to a caller: ask
        the database.
        """
        ...

    async def put_ids(self, player_id: UUID, entry: SocialGraphEntry, ids: frozenset[UUID]) -> None:
        """Stores an id set with the configured TTL.

        The TTL is a backstop for invalidation failing, never the mechanism
        (caching.md C-3). A failure to store is silent: the next read simply
        misses.
        """
        ...

    async def invalidate(self, player_ids: Sequence[UUID]) -> None:
        """Drops every cached entry for these players.

        Called with **both** parties of whatever changed, because a
        friendship and a block are facts about a pair.

        A failure here is a *correctness* problem rather than a performance
        one — it leaves a removed friend visible or a lifted block in
        effect until the TTL — so the adapter logs it at `ERROR`. It still
        does not raise: the database is the system of record, and a block
        that failed to invalidate must not also fail to be placed.
        """
        ...
