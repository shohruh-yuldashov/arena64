"""The ports `friends` programs against — AD-06: declared in
`application/`, satisfied by `infrastructure/`.

One port. This module owns its own storage, so unlike `profiles` there is no
cross-context read here and nothing to adapt — `FriendRequestRepository` is
the whole surface between the use cases and PostgreSQL.

There is deliberately no `FriendshipRepository` and no `BlockRepository`.
A64-013.2 implements neither, and a port with no implementation is a hole
that reads as supported (`statistics.application.ports` makes the same
argument about a writer that does not exist yet).
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.modules.friends.domain.friend_request import FriendRequest, FriendRequestStatus


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
