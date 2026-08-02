"""`QueueCooldown` — a player barred from the queue for a while, and why.

Framework-free (architecture.md §8): no SQL, no clock. The instant arrives
as an argument (AD-07), so the whole window is a unit test that runs in a
microsecond.

## What this is for, and what it is deliberately not

A64-015.5 §3 asks for a cooldown on an **explicit decline**. A player who
is offered a match and refuses it has cost somebody else a wait, and the
cooldown is what stops a client — or a person — cycling the queue until it
produces an opponent they like the look of. That is a rating-manipulation
vector, and it is the same one `RATE_LIMIT_MATCHMAKING_QUEUE_USER_LIMIT`
bounds from a different direction: the rate limit bounds *how often* you can
act, this bounds *what your action costs you*.

It is **not** a sanction and it is not moderation. There is no appeal, no
record beyond its own expiry, and no accumulation across days — a second
decline extends the window rather than escalating a penalty. Anything that
should escalate belongs to `admin`, which does not exist, and building a
strike counter here would put a moderation policy in the module least
entitled to own one.

## Silence is not a decline

§3 is explicit: "silent expiry is not automatically treated as decline
unless product policy explicitly says so". It does not, so it is not. A
player whose window closed without an answer earns **no cooldown at all**,
and `CooldownReason` has exactly one member to make that structural rather
than remembered — there is no value this type can hold that means "they
said nothing".

The asymmetry is deliberate and is the safe direction. A decline is an
observed decision; silence has a dozen causes the platform cannot
distinguish (a dead battery, a tunnel, a crashed tab, a person who walked
away), and punishing all of them for the one that deserves it would make the
queue hostile to anybody on a train.

## One live cooldown per player, and extension rather than accumulation

The storage key is the player (see `matchmaking.queue_cooldown`), so a
second decline **extends** the existing window to whichever end is later
rather than inserting a second row. Two reasons:

  - a set of overlapping cooldowns has to be reduced to one instant at
    every read, and the reduction is `max()` — so storing them separately
    is storing the same answer several times;
  - "repeated decline does not bypass the cooldown" (§3) is then a property
    of one `UPDATE`, rather than of a query that has to remember to take
    the maximum.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class CooldownReason(StrEnum):
    """Why a player may not queue right now.

    **One member**, and that is the point rather than an unfinished
    enumeration: it is what makes "silence earns no cooldown" impossible to
    violate by accident — see this module's docstring.

    A native PostgreSQL enum on the column (DB-15). A second reason is a
    migration and a decision, which is the right cost for adding a way to
    keep somebody out of the queue.
    """

    DECLINED_MATCH = "declined_match"
    """They were offered a match and explicitly refused it."""


@dataclass(frozen=True, slots=True)
class QueueCooldown:
    """One player's bar on joining a pool, and when it lifts.

    Frozen, like `QueueTicket` and `MatchRecord` and for the same reason:
    the write that persists it is an upsert whose `SET` clause needs the
    *new* value, and the read that enforces it needs the stored one. A
    mutable object would blur which of the two a caller is holding.
    """

    player_id: UUID
    """Whose cooldown. An opaque cross-context identifier (DM-06) — no
    foreign key, and nothing here can resolve it to a person."""

    reason: CooldownReason
    expires_at: datetime
    """When the bar lifts.

    Absolute rather than a duration, for the reason `QueueTicket.expires_at`
    is: a cooldown written under one `MATCHMAKING_DECLINE_COOLDOWN_SECONDS`
    must not be silently re-dated by a deploy that changes it.
    """

    created_at: datetime
    """When it was applied. Not used to decide anything — `expires_at` is
    the rule — and kept because "how long was this cooldown" is the only
    question an operator investigating a complaint can ask, and it is not
    derivable from an end instant alone."""

    def __post_init__(self) -> None:
        # Re-checked here rather than only at construction, because the
        # repository builds instances directly when rehydrating — this is
        # what makes a corrupt row fail at the boundary rather than reach a
        # response. The database's CHECK is the authoritative copy (BE-06).
        if self.expires_at <= self.created_at:
            raise ValueError("a cooldown cannot expire before it was applied")

    @classmethod
    def after_decline(cls, player_id: UUID, *, at: datetime, seconds: float) -> "QueueCooldown":
        """The cooldown an explicit decline earns.

        A named constructor rather than a bare `QueueCooldown(...)`, so the
        one reason this type has is bound to the one event that produces it
        — a caller cannot apply a `declined_match` cooldown for something
        that was not a decline without saying so in the call.

        `seconds` is configuration and `at` is injected (AD-07), so the
        whole window is testable without waiting for it.
        """
        if seconds <= 0:
            raise ValueError("a cooldown must be a positive number of seconds")
        return cls(
            player_id=player_id,
            reason=CooldownReason.DECLINED_MATCH,
            expires_at=at + timedelta(seconds=seconds),
            created_at=at,
        )

    def is_active(self, at: datetime) -> bool:
        """Whether the bar still applies at `at`.

        Strict on the boundary (`<`), unlike `QueueTicket.is_due`'s `>=`,
        and the asymmetry is deliberate: a ticket's deadline is the platform
        withdrawing an offer, so the instant itself belongs to the platform;
        a cooldown's is a restriction lifting, so the instant belongs to the
        player. A player refused at exactly the microsecond their cooldown
        ends would be refused for a bar that had expired.
        """
        return at < self.expires_at

    def remaining(self, at: datetime) -> float:
        """Seconds until the bar lifts, floored at zero.

        The number a client renders and the one `Retry-After` carries.
        Floored rather than allowed negative, because "retry in minus four
        seconds" is not something any caller can act on.
        """
        return max((self.expires_at - at).total_seconds(), 0.0)

    def extended_to(self, other: "QueueCooldown") -> "QueueCooldown":
        """This cooldown, or `other`, whichever ends later.

        The reduction the storage upsert performs, expressed here so the
        rule lives in the domain and the repository merely applies it —
        and so "a repeated decline does not bypass the cooldown" (§3) is
        testable without a database.

        Keeps the **original** `created_at`, because the question it answers
        is "when did this player start being barred", and a second decline
        does not restart that.
        """
        if other.player_id != self.player_id:
            raise ValueError("cooldowns for two different players cannot be merged")
        if other.expires_at <= self.expires_at:
            return self
        return QueueCooldown(
            player_id=self.player_id,
            reason=other.reason,
            expires_at=other.expires_at,
            created_at=self.created_at,
        )


__all__ = ["CooldownReason", "QueueCooldown"]
