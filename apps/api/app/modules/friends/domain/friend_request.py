"""`FriendRequest` — a proposal of relationship and its resolution.

Framework-free (architecture.md §8). No SQL, no clock, no session: time
arrives as an argument (AD-07), and every method here is a pure state
transition over data the caller already holds.

domain-model.md §8.1: "Records a proposal of relationship and its
resolution. **The proposal is a fact independent of its outcome.**" That
sentence is the design. A declined request is not a failed request that
should be cleaned up — it is a thing that happened, and FR-5's decline
cooldown is a rule that reads it. Nothing here deletes.

## Why this is an aggregate root and not a row

It owns an invariant no other object can enforce: **exactly one transition
out of `PENDING`, made by exactly one party.** Accept, decline and cancel
are all "resolve this request", differing only in who may do it and what
the outcome is called, and every one of them must refuse a request that has
already been resolved.

A service could check `status is PENDING` before each write, and would be
correct until the second caller. The check belongs with the data it
constrains, which is what makes `_resolve` below the only way to leave
`PENDING` and `version` the thing that makes it safe under concurrency.

## Expiry, which is not implemented and is not an afterthought

domain-model.md's lifecycle includes `Pending -> Expired: no response
within the retention window`, and A64-013.2 excludes it. The brief asks
that adding it later needs no redesign, so:

  - `EXPIRED` is **a member of the status enum from this release**, so the
    database's enum type already contains it and adding expiry is not a
    migration of a live type used by a hot column;
  - `expires_at` is **a column and a field from this release**, nullable,
    and `None` means "no window" rather than "never expires" — those read
    the same today and will not once a default window exists;
  - `_resolve` takes the resolving instant, so an expiry sweep is a caller
    that resolves to `EXPIRED` rather than a second write path.

What is missing is only the *policy*: what the window is, and what runs the
sweep. Neither is a change to this file.

`VOIDED` is here for the same reason, one step further out: FR-2 and BL-2
make a block void any pending request between the two players, and
A64-013.5 will need the state to exist before it can write it.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.friends.domain.exceptions import (
    FriendRequestAlreadyResolved,
    FriendRequestNotPending,
    NotRequestAddressee,
    NotRequestRequester,
    SelfFriendRequest,
)


class FriendRequestStatus(StrEnum):
    """Where a request is in domain-model.md §8.1's lifecycle.

    A `StrEnum` so the stored value, the wire value and the Python member
    are one string — the choice `VisibilityLevel` and `BoardTheme` make, for
    the same reason.

    **Six members, three of which nothing produces yet.** `EXPIRED` and
    `VOIDED` are declared now so the PostgreSQL enum type contains them
    before anything needs to write one; adding a value to an enum used by an
    indexed column on a live table is a migration nobody should have to
    schedule to ship a feature. See this module's docstring.
    """

    PENDING = "pending"
    """Sent, unresolved. The only state with an outgoing transition, and the
    only one the uniqueness rule constrains (FR-1)."""

    ACCEPTED = "accepted"
    """The addressee agreed. A64-013.3 makes this create the `Friendship` in
    the same transaction (FR-4) — this release records the resolution and
    stops there, which is why accepting today produces no friend list."""

    DECLINED = "declined"
    """The addressee refused. **Silent to the requester** (FR-3): nothing
    notifies them, and the row is kept because FR-5's cooldown reads it."""

    CANCELLED = "cancelled"
    """The requester withdrew.

    domain-model.md calls this `Withdrawn`. A64-013.2 specifies `cancelled`
    and the API says `DELETE`, so the brief's word wins on the wire and in
    storage; the divergence is recorded here rather than resolved silently,
    because the design document and the code disagreeing without a note is
    how somebody later "fixes" one of them.
    """

    EXPIRED = "expired"
    """No response within the retention window. **Nothing produces this
    yet** — A64-013.2 excludes expiry. See this module's docstring."""

    VOIDED = "declined_by_block"
    """Either party blocked the other (FR-2, BL-2). **Nothing produces this
    yet** — A64-013.5 will.

    The stored value differs from the member name deliberately: `voided` is
    the domain's word and says nothing to an operator reading a row, while
    `declined_by_block` says why the request ended. A client never sees it
    either way — see `presentation/schemas`, which does not publish this
    member.
    """

    @property
    def is_resolved(self) -> bool:
        """Whether this is a terminal state.

        Defined as "not pending" rather than as a list of the five terminal
        members, so a seventh state added later is terminal by default. That
        is the safe direction: a new state that should have been resolvable
        fails loudly the first time somebody tries to resolve it, while a
        new state accidentally treated as pending would let a resolved
        request be resolved twice.
        """
        return self is not FriendRequestStatus.PENDING


@dataclass(slots=True)
class FriendRequest:
    """One proposal, from `requester_id` to `addressee_id`.

    **Mutable, unlike most value-shaped types on this platform**, because it
    is an aggregate root with a lifecycle rather than a snapshot: a
    transition changes the identity's state, it does not produce a second
    request. `PrivacySettings` is frozen for the opposite reason.

    Constructed only through `send` or by the repository rehydrating a row.
    The `__init__` a dataclass generates is deliberately not the sanctioned
    entry point — it cannot enforce FR's self-request rule, which is why
    `send` exists and why `__post_init__` re-checks it for the repository's
    path.
    """

    requester_id: UUID
    """Who sent it. Ordered, not canonical: `friend_request` is directional,
    unlike `friendship`, which stores one row per *unordered* pair
    (database.md §7.3's canonical-pair pattern). A request from A to B and
    one from B to A are different facts, and the opposite-direction rule
    below is a *business* rule rather than a storage one for exactly that
    reason."""

    addressee_id: UUID
    """Who received it, and the only account that may accept or decline."""

    status: FriendRequestStatus = FriendRequestStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.min)
    responded_at: datetime | None = None
    """When the request left `PENDING`. `None` exactly while pending — a
    database CHECK enforces the pairing (BE-06), so a row cannot claim to be
    accepted without saying when."""

    expires_at: datetime | None = None
    """When this request lapses, or `None` for no window.

    **Always `None` today**: A64-013.2 excludes expiry. Declared now so that
    adding it is a policy change and a backfill rather than a schema change
    to a table with live rows — see this module's docstring.

    `None` means *no window*, not *never expires*. The two read the same
    while nothing sweeps, and stop reading the same the moment something
    does: a sweep must skip rows with no window rather than treat them as
    infinitely old.
    """

    version: int = 0
    """Optimistic-concurrency token — repositories.md §8.4 names
    `FriendRequest` status transitions as one of exactly two places on the
    platform needing one.

    The race is real and has a visible wrong outcome: an addressee with the
    request open on a phone and a laptop can accept on one and decline on
    the other. Without this, both writes succeed and the row's final state
    is whichever landed second, with `responded_at` from the other. With it,
    the second write matches no row and the caller is told the request has
    already been resolved — which is true.
    """

    id: UUID = field(default_factory=generate_uuid7)
    """UUIDv7, application-generated (DB-07), last so every other field can
    be passed positionally in the repository's rehydration."""

    def __post_init__(self) -> None:
        if self.requester_id == self.addressee_id:
            # Re-checked here rather than only in `send`, because the
            # repository constructs instances directly when rehydrating a
            # row. A database CHECK enforces the same rule (BE-06); this is
            # what makes a corrupt row fail at the boundary rather than
            # reaching a response.
            raise SelfFriendRequest("A player cannot send a friend request to themselves.")

    @classmethod
    def send(cls, *, requester_id: UUID, addressee_id: UUID, sent_at: datetime) -> "FriendRequest":
        """A new pending request.

        Enforces only the rule this aggregate can see on its own — that the
        two parties differ. The three rules that need to look at *other*
        rows (no duplicate pending, no opposite pending, no blocked pair)
        belong to `FriendRequestValidator`, which has a repository; putting
        them here would mean handing the aggregate one.

        `sent_at` is injected rather than read (AD-07): a request's age is
        what FR-5's cooldown and the future expiry window are both computed
        from, and a test asserting either must not have to sleep.
        """
        return cls(
            requester_id=requester_id,
            addressee_id=addressee_id,
            status=FriendRequestStatus.PENDING,
            created_at=sent_at,
        )

    def accept(self, *, by: UUID, at: datetime) -> None:
        """The addressee agrees.

        Raises `NotRequestAddressee` when anybody else tries — including the
        requester, who may cancel but must never be able to accept their own
        request. That is the ownership check A64-013.2 asks for, and it
        lives here rather than in the service so that no future caller can
        reach a transition without it.
        """
        self._require_addressee(by)
        self._resolve(FriendRequestStatus.ACCEPTED, at=at)

    def decline(self, *, by: UUID, at: datetime) -> None:
        """The addressee refuses. Silent to the requester (FR-3).

        Same ownership rule as `accept`: the two differ only in the status
        they land on, which is why both go through `_resolve`.
        """
        self._require_addressee(by)
        self._resolve(FriendRequestStatus.DECLINED, at=at)

    def cancel(self, *, by: UUID, at: datetime) -> None:
        """The requester withdraws.

        The one transition the *sender* owns, and the reason `_resolve` takes
        the actor check as a separate step rather than assuming it: an
        addressee must not be able to cancel a request they could simply
        decline, because the two leave different history and FR-5 reads it.
        """
        self._require_requester(by)
        self._resolve(FriendRequestStatus.CANCELLED, at=at)

    def _resolve(self, status: FriendRequestStatus, *, at: datetime) -> None:
        """The only way out of `PENDING`.

        Every transition funnels here so that "a resolved request cannot be
        resolved again" is enforced once. The alternative — the same guard
        written in `accept`, `decline` and `cancel` — is three copies of an
        invariant, and the fourth transition (expiry) would be written by
        somebody reading only one of them.

        `responded_at` is set in the same statement as `status`, which is
        what the database's `responded_iff_resolved` CHECK asserts
        independently: the row cannot record an outcome without its instant.
        """
        if self.status.is_resolved:
            raise FriendRequestAlreadyResolved("This friend request has already been resolved.")

        self.status = status
        self.responded_at = at

    def _require_addressee(self, actor: UUID) -> None:
        if actor != self.addressee_id:
            # The message says what the *actor* may do, never who the other
            # party is. A rejection that named the requester would turn a
            # guessed request id into a way to learn who is sending friend
            # requests to whom.
            raise NotRequestAddressee("Only the recipient of a friend request may respond to it.")

    def _require_requester(self, actor: UUID) -> None:
        if actor != self.requester_id:
            raise NotRequestRequester("Only the sender of a friend request may cancel it.")

    def require_pending(self) -> None:
        """Guard for a caller that needs the pending state without resolving
        it.

        Used by nothing in A64-013.2 and published rather than private
        because A64-013.5's block handler is the caller: voiding a request
        must skip rows that are already resolved, and it needs to ask
        without attempting a transition. One line, and it keeps
        `FriendRequestNotPending` a domain concept rather than a service's
        local `if`.
        """
        if self.status.is_resolved:
            raise FriendRequestNotPending("This friend request is no longer pending.")
