"""`Friendship` — one mutual relationship, stored once in canonical order.

Framework-free (architecture.md §8). No SQL, no clock: time arrives as an
argument (AD-07), and the ordering rule below is pure arithmetic over two
identifiers.

domain-model.md §8.2: "A mutual, symmetric relationship with its own start
date. It gates presence visibility, direct challenges, direct messaging, and
friend-scoped leaderboards." As of A64-013.3 it gates the first of those —
`VisibilityLevel.FRIENDS` is no longer a value nothing can satisfy.

## The canonical pair, and why it is a type rather than a convention

DB-12: symmetric relationships are stored **once**, in canonical identifier
order, with a check constraint making any other ordering unrepresentable.

"Two rows for one relationship is two facts that can disagree, and when they
do, neither is authoritative — there is no principled repair." The obvious
alternative, mirroring, buys read convenience and pays for it in
correctness; DB-12 buys the same convenience with two indexes on one row
(§12.3), which costs index space instead.

A convention would fail exactly once — silently, in production, under the
concurrency that produced the out-of-order write. So the ordering happens in
`between` below, which is the only sanctioned constructor, and
`__post_init__` re-checks it for the repository's rehydration path. The
database's `ck_friendship__canonical_order` is the authoritative third copy
(BE-06).

## Why this is an aggregate root and not a join row

It has its own lifecycle and its own start date. domain-model.md §8.2 is
explicit about why it is not a collection on `UserProfile`: "a friend list
held inside a profile makes acceptance a two-aggregate write (both
profiles), which cannot be one transaction without locking two players'
profile rows — on a platform where profile rows are read on every page
render. The relationship is its own thing, owned by neither party."

## Ending is recorded, not deleted

FS-2: "Removal is unilateral and silent." A64-013.3 implements the
unilateral half; the silence is a property of what this platform does *not*
do — nothing notifies the other party.

`ended_at` rather than a `DELETE`, per database.md §1221: "a friendship that
ended is a fact with a date; the row is history, not debris". It is also
what makes re-friending possible — the unique index is partial on live rows,
so a pair whose friendship ended can form a new one, which a plain unique
would have forbidden forever.

`ended_reason` is a column and a field from this release with exactly one
producer (`REMOVED`). A64-013.5's block sets `BLOCKED` (FS-3: "a block
immediately voids any friendship"), which is then a policy change rather
than a migration of a live enum on an indexed table.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.friends.domain.exceptions import (
    FriendshipAlreadyEnded,
    NotFriendshipParticipant,
    SelfFriendship,
)


class FriendshipEndReason(StrEnum):
    """Why a friendship stopped being live.

    A `StrEnum` so the stored value, the wire value and the Python member
    are one string — the choice every other enum on this platform makes.

    **Two members, one of which nothing produces yet.** `BLOCKED` is
    declared now so the PostgreSQL enum type contains it before anything
    needs to write one: `ALTER TYPE ... ADD VALUE` on a type used by a live
    table is a migration nobody should have to schedule to ship a feature.
    A64-013.5 is what will write it.
    """

    REMOVED = "removed"
    """Either party removed the other. Unilateral and silent (FS-2)."""

    BLOCKED = "blocked"
    """Either party blocked the other (FS-3). **Nothing produces this yet** —
    A64-013.5 will, and BL-2 is why it must be a distinct reason rather than
    a second `REMOVED`: a blocked pair must not be able to re-friend, and
    that rule reads this column."""


@dataclass(slots=True)
class Friendship:
    """One live or ended relationship between two players.

    **Mutable**, like `FriendRequest` and unlike most value-shaped types
    here, because it is an aggregate root with a lifecycle: ending it
    changes this relationship's state rather than producing a second one.

    Constructed only through `between` or by the repository rehydrating a
    row. The generated `__init__` is not the sanctioned entry point — it
    cannot sort the pair, which is why `between` exists and why
    `__post_init__` re-checks the ordering.
    """

    player_low_id: UUID
    """The numerically smaller of the two identifiers.

    "Low" and "high" carry no meaning beyond ordering — neither player is
    the owner, the initiator or the senior party. The relationship is
    symmetric (FS-1) and the names exist only so one row can represent it.
    """

    player_high_id: UUID
    """The numerically larger of the two identifiers."""

    created_at: datetime = field(default_factory=lambda: datetime.min)
    """When the friendship began — the instant the request was accepted, and
    the same instant recorded on that request's `responded_at`, because both
    are written in one transaction (FR-4)."""

    source_request_id: UUID | None = None
    """The `FriendRequest` whose acceptance created this, per database.md
    §7.3.

    Nullable because a future friendship might arrive another way — an
    import, an administrative action — and because a request that has been
    purged under a retention policy must not take the friendship with it.
    Carried rather than inferred: "which request led to this" is not
    derivable once a second request between the same pair exists.
    """

    ended_at: datetime | None = None
    """When the friendship stopped being live, or `None` while it is.

    `None` exactly when `ended_reason` is — a database CHECK enforces the
    pairing (BE-06), so a row cannot claim to have ended without saying why
    or why without saying when.
    """

    ended_reason: FriendshipEndReason | None = None

    id: UUID = field(default_factory=generate_uuid7)
    """UUIDv7, application-generated (DB-07). Last so every other field can
    be passed positionally by the repository's rehydration."""

    def __post_init__(self) -> None:
        if self.player_low_id == self.player_high_id:
            raise SelfFriendship("A player cannot be their own friend.")
        if self.player_low_id > self.player_high_id:
            # Re-checked here rather than only in `between`, because the
            # repository constructs instances directly when rehydrating.
            # `ck_friendship__canonical_order` enforces the same rule
            # authoritatively (BE-06); this is what makes a mis-ordered row
            # fail at the boundary rather than reaching a response — and
            # what would catch a hand-written `INSERT` in a repair script.
            raise ValueError(
                "friendship identifiers are stored in canonical order; "
                "player_low_id must be less than player_high_id"
            )

    @property
    def is_live(self) -> bool:
        """Whether this friendship currently exists.

        Defined as "not ended" rather than by listing the end reasons, so a
        third reason added later is terminal by default — the safe
        direction, matching `FriendRequestStatus.is_resolved`.
        """
        return self.ended_at is None

    @classmethod
    def between(
        cls,
        player_a: UUID,
        player_b: UUID,
        *,
        created_at: datetime,
        source_request_id: UUID | None = None,
    ) -> "Friendship":
        """A new live friendship between two players, in canonical order.

        **Takes the pair unordered and sorts it**, which is the whole point:
        no caller has to know about `low` and `high`, and no caller can get
        them wrong. `FriendRequestService` passes requester and addressee in
        whatever order the request happened to have.

        Raises `SelfFriendship` if the two are the same player — unreachable
        through the request flow, since `FriendRequest` refuses a
        self-request three ways, but this is the aggregate that would have
        to be wrong for it to happen.

        `created_at` is injected rather than read (AD-07), and on this path
        it is the *same instant* the request's `responded_at` records —
        they are one event and are written in one transaction.
        """
        low, high = canonical_pair(player_a, player_b)
        return cls(
            player_low_id=low,
            player_high_id=high,
            created_at=created_at,
            source_request_id=source_request_id,
        )

    def end(self, *, by: UUID, at: datetime, reason: FriendshipEndReason) -> None:
        """Ends the friendship. **Unilateral** — FS-2.

        Either participant may do this and neither needs the other's
        agreement: "requiring mutual agreement to stop being friends is not
        a feature anyone wants." The ownership check is therefore
        *participation*, not ownership in the usual sense — there is no
        owner, and `by` must simply be one of the two.

        Raises `NotFriendshipParticipant` for anybody else, and
        `FriendshipAlreadyEnded` for a friendship that is no longer live —
        the same shape `FriendRequest._resolve` has, and for the same
        reason: exactly one transition out of the live state.

        The `by` parameter is not stored. Who ended a friendship is not a
        fact either party is entitled to learn, and recording it would make
        the row answer a question FS-2's silence exists to leave unanswered.
        It is taken so the check can be made here, in the aggregate, rather
        than in a service where a later caller could skip it.
        """
        self.require_participant(by)

        if not self.is_live:
            raise FriendshipAlreadyEnded("This friendship has already ended.")

        self.ended_at = at
        self.ended_reason = reason

    def require_participant(self, player_id: UUID) -> None:
        """Guard: `player_id` is one of the two, or raise.

        Published rather than private because A64-013.4's management
        endpoints and A64-013.5's block handler both need to ask without
        attempting a transition — the same reason
        `FriendRequest.require_pending` is public.

        The message names neither participant. A rejection that did would
        turn a guessed identifier into a way to learn who is friends with
        whom, which is precisely what a friends-only visibility setting
        exists to control.
        """
        if player_id not in (self.player_low_id, self.player_high_id):
            raise NotFriendshipParticipant("You are not part of this friendship.")

    def other_than(self, player_id: UUID) -> UUID:
        """The participant who is not `player_id`.

        What a friend *list* renders: the viewer knows who they are and
        wants the other person. Raises `NotFriendshipParticipant` if asked
        about somebody who is not in the pair, rather than returning an
        arbitrary side — a silent wrong answer here would render a stranger
        as a friend.
        """
        self.require_participant(player_id)
        return self.player_high_id if player_id == self.player_low_id else self.player_low_id


def canonical_pair(player_a: UUID, player_b: UUID) -> tuple[UUID, UUID]:
    """The two identifiers, smallest first — DB-12's ordering.

    A module-level function rather than a method, because every *query*
    needs it too and none of them has a `Friendship` to call it on:
    `exists`, `remove` and `friends_of` all have to reduce an unordered pair
    to the canonical one before they can touch an index.

    One definition, three call sites, and the check constraint as the
    backstop. Two definitions would be the failure DB-12 describes: the
    invariant holding everywhere except the one path that sorted
    differently.
    """
    return (player_a, player_b) if player_a < player_b else (player_b, player_a)
