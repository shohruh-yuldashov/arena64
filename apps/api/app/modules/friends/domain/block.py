"""`Block` — a unilateral, asymmetric refusal of contact.

Framework-free (architecture.md §8). No SQL, no clock: time arrives as an
argument (AD-07).

domain-model.md §8.3. Three rules govern it and all three are visible in
what this type does *not* have:

    BL-1  asymmetric and one-directional; the blocked player is never told
    BL-2  a block suppresses friend requests, direct challenges, direct
          messages, presence visibility, and matchmaking pairing
    BL-3  blocks do not rewrite history

**No lifecycle.** Unlike `Friendship` and `FriendRequest`, this aggregate
has no states and no transitions: it exists or it does not. Unblocking is a
`DELETE`, not an `ended_at`, and database.md §7.2 is explicit about why —
"a block has no history worth keeping, and retaining released blocks would
make BL-2's matchmaking filter, already the most performance-sensitive use
of this relation, read rows it must then exclude."

That is the opposite decision from `Friendship`, which is soft-ended, and
the difference is real: a friendship that ended is a fact two people
participated in, while a block that was lifted is one person's private
change of mind.

**No `reason` and no `note`.** A block is not moderation. `admin` owns
sanctions with their evidence and their audit trail (domain-model.md §37);
this is a player deciding who may contact them, and a free-text field on it
would be a place for a grievance to be recorded about somebody who cannot
see it and cannot answer it.

## Why the pair is ordered, unlike `Friendship`

`Friendship` stores one row per *unordered* pair in canonical order (DB-12),
because the relationship is symmetric and two rows could disagree. A block
is directional: A blocking B and B blocking A are two different facts, both
of which can be true, and neither implies the other. So the pair is stored
as given, `(blocker_id, blocked_id)`, and the unique constraint covers that
ordered pair.

The *visibility* consequence is symmetric even though the fact is not — see
`ViewerRelationship.BLOCKED` — which is a rule the relationship provider
applies, not something this type knows.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.friends.domain.exceptions import SelfBlock


@dataclass(frozen=True, slots=True)
class Block:
    """One player's refusal to be contacted by another.

    **Frozen**, unlike the two aggregates beside it, and for the reason
    given above: there is no transition to make. A block is created and
    deleted; nothing about it ever changes.
    """

    blocker_id: UUID
    """The player who blocked. The only account that may lift it."""

    blocked_id: UUID
    """The player who was blocked, and who is never told (BL-1)."""

    created_at: datetime = field(default_factory=lambda: datetime.min)
    """When the block was placed.

    Kept even though nothing reads it today, because it is the one thing an
    operator investigating a report needs and cannot reconstruct — and it
    costs eight bytes on a relation BL-4 already bounds.
    """

    id: UUID = field(default_factory=generate_uuid7)
    """UUIDv7, application-generated (DB-07). Last so every other field can
    be passed positionally by the repository's rehydration."""

    def __post_init__(self) -> None:
        if self.blocker_id == self.blocked_id:
            # Re-checked here rather than only in `place`, because the
            # repository constructs instances directly when rehydrating.
            # `ck_blocked_player__not_self` enforces the same rule
            # authoritatively (BE-06); this is what makes a corrupt row fail
            # at the boundary rather than reaching a response.
            raise SelfBlock("A player cannot block themselves.")

    @classmethod
    def place(cls, *, blocker_id: UUID, blocked_id: UUID, at: datetime) -> "Block":
        """A new block.

        Enforces only the rule this aggregate can see on its own — that the
        two parties differ. Duplicate prevention is the unique index's,
        because two concurrent blocks both pass a check-then-act (BE-06),
        and the *cascade* (ending a friendship, voiding requests) is
        `BlockingService`'s, because it spans three relations.

        `at` is injected rather than read (AD-07), and on the blocking path
        it is the same instant the friendship's `ended_at` and the voided
        requests' `responded_at` record — they are one event.
        """
        return cls(blocker_id=blocker_id, blocked_id=blocked_id, created_at=at)
