"""`CooldownRecord` — why a player is barred, kept after the bar has lifted.
A64-015.6 §3.

A64-015.5 shipped `queue_cooldown` keyed on the player, with a second decline
**extending** the row rather than inserting one. That is the right shape for
*enforcement* — one row, one `GREATEST` upsert, no reduction at read time —
and it has a consequence that task recorded and did not solve:

> "What it costs is history: a second decline overwrites the first's
> `expires_at` and nothing records that there were two."

A player disputing a cooldown therefore had no evidence, and support had no
query. §3 asks for that, and asks for it *without* turning a queue delay into
a disciplinary file.

## Append-only, beside the enforcement row rather than inside it

Two relations, and the split is by what they are *for*:

    queue_cooldown        one row per player. Answers "may this player queue
                          right now" in a primary-key lookup on the join path
    queue_cooldown_audit  one row per cooldown-causing event. Answers "why,
                          and how did we get here" for a human

Merging them would mean either losing history (today's behaviour) or making
the enforcement read a scan over a player's whole history with a `max()` — on
the hot path, for a question the primary key already answers.

## This is not a Sanction, and it must not become one

§3 is explicit that this "must not reuse the moderation Sanction model", and
the reason is worth stating rather than obeying: a sanction is a *judgement*
— it has an author, an appeal, a severity and a duration somebody chose. A
cooldown is a **mechanical consequence** of one action, with a duration from
a settings file and nobody's name on it.

So this record has no actor, no severity, no note field and no escalation
count. It is a log of "the platform did this, for this reason, at this time",
and the day `admin` exists a sanction will reference a player rather than
extend this.

`CooldownReason` is deliberately shared with `QueueCooldown` rather than
widened here: a record whose reason the enforcement row cannot hold would be a
record of something that never happened.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.core.identifiers import generate_uuid7
from app.modules.matchmaking.domain.cooldown import CooldownReason, QueueCooldown


@dataclass(frozen=True, slots=True)
class CooldownRecord:
    """One cooldown, as it was applied, kept whatever happened afterwards.

    Frozen and never updated. An audit row that could be edited is an audit
    row that answers "what does the platform say now" rather than "what
    happened", which is the only question it exists for.
    """

    player_id: UUID
    """Whose bar. An opaque cross-context identifier (DM-06) — support
    resolves it through `users`, and nothing here can."""

    reason: CooldownReason
    source_match_id: UUID | None
    """The pending match whose decline caused this, when there was one.

    **The idempotency key**, paired with the player: a unique index on
    `(player_id, source_match_id)` means a redelivered `game.match_declined`
    writes one row rather than two. See `CooldownAuditRepository.record`.

    `None` is legal and is reserved for a future reason that is not a
    per-match decline. Nulls are distinct in a unique index, so such rows do
    not contend — which is correct: two cooldowns from two non-match causes
    are two events.
    """

    applied_at: datetime
    expires_at: datetime
    """When the bar was applied, and when it lifted or will lift.

    Both absolute. `expires_at` is the value that was *written to the
    enforcement row*, which after an extension is not the same as
    `applied_at + the configured window` — so a support answer built from
    this pair is the truth rather than an arithmetic reconstruction.
    """

    extended_existing: bool
    """Whether this decline lengthened a bar that was already in force.

    The one derived field, and it earns its place: it is the whole of what
    A64-015.5 lost, and computing it later would need the previous row and an
    ordering. `True` here is what a support answer means by "they had already
    declined one".

    Derived from whether a bar was **in force when this landed**, not from
    comparing expiries — see `MatchOutcomeService._cool_down` on why the
    second reading misses the ordinary repeat offender.
    """

    id: UUID = field(default_factory=generate_uuid7)
    """UUIDv7, application-generated (DB-07). Last so every other field can be
    passed positionally by the repository's rehydration."""

    def __post_init__(self) -> None:
        # Re-checked here rather than only at construction, because the
        # repository builds instances directly when rehydrating — this is
        # what makes a corrupt row fail at the boundary rather than reach a
        # support screen. The database's CHECK is the authoritative copy
        # (BE-06).
        if self.expires_at <= self.applied_at:
            raise ValueError("a cooldown record cannot expire before it was applied")

    @classmethod
    def of(
        cls,
        cooldown: QueueCooldown,
        *,
        source_match_id: UUID | None,
        applied_at: datetime,
        extended_existing: bool,
    ) -> "CooldownRecord":
        """The audit row for a cooldown that was just stored.

        Takes the **stored** cooldown rather than the one that was requested,
        so `expires_at` is what is actually in force — after an extension
        those differ, and the difference is exactly what a dispute is about.

        `applied_at` is passed separately rather than read off the cooldown:
        the stored row keeps its *original* `created_at` across an extension
        (that is `QueueCooldown.extended_to`'s rule), and this record is about
        *this* decline.
        """
        return cls(
            player_id=cooldown.player_id,
            reason=cooldown.reason,
            source_match_id=source_match_id,
            applied_at=applied_at,
            expires_at=cooldown.expires_at,
            extended_existing=extended_existing,
        )

    def was_active_at(self, instant: datetime) -> bool:
        """Whether this bar applied at `instant`.

        The question a support conversation actually starts with — "I could
        not queue at half past two" — and it is answered from the record
        rather than from the live enforcement row, which by then has expired
        and been pruned.
        """
        return self.applied_at <= instant < self.expires_at


__all__ = ["CooldownRecord"]
