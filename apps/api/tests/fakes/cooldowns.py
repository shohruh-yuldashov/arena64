"""An in-memory `CooldownRepository` — A64-015.5.

What is faked is **storage**, never the thing under test.
`CooldownEligibilityPolicy` and `MatchOutcomeService` run for real against
this, so the decline-versus-silence rule and the join-path refusal are
genuinely exercised.

## The one behaviour that is modelled rather than simplified

`apply` takes the **later** expiry, which is what the real adapter's
`ON CONFLICT DO UPDATE ... GREATEST(...)` does. It is modelled because §3's
"a repeated decline does not bypass the cooldown" is exactly this rule, and
a fake that overwrote would leave it untested on the path that enforces it.

What is *not* modelled is the atomicity: two concurrent `apply` calls
against real PostgreSQL resolve inside one statement, and here they would
interleave. That property belongs to the database and is asserted where it
can be — `tests/contract/test_cooldown_repository.py`, with two real
sessions — for the same reason `tests/fakes/queue_repository.py` declines to
reimplement `SKIP LOCKED`.
"""

from datetime import datetime
from uuid import UUID

from app.modules.matchmaking.domain.cooldown import QueueCooldown


class InMemoryCooldownRepository:
    """The `matchmaking.queue_cooldown` relation, as a dict keyed on the
    player — which is what the real primary key is."""

    def __init__(self) -> None:
        self.cooldowns: dict[UUID, QueueCooldown] = {}

    async def apply(self, cooldown: QueueCooldown) -> QueueCooldown:
        existing = self.cooldowns.get(cooldown.player_id)
        merged = cooldown if existing is None else existing.extended_to(cooldown)
        self.cooldowns[cooldown.player_id] = merged
        return merged

    async def active_for(self, player_id: UUID, *, now: datetime) -> QueueCooldown | None:
        """Expiry is applied on read, exactly as the real query applies it:
        a lapsed row that retention has not reached must read as absent, or
        a player would be refused by bookkeeping rather than by a rule."""
        cooldown = self.cooldowns.get(player_id)
        return cooldown if cooldown is not None and cooldown.is_active(now) else None

    async def prune_expired(self, *, before: datetime, batch_size: int) -> int:
        lapsed = sorted(
            (cooldown for cooldown in self.cooldowns.values() if cooldown.expires_at <= before),
            key=lambda cooldown: cooldown.expires_at,
        )[:batch_size]
        for cooldown in lapsed:
            del self.cooldowns[cooldown.player_id]
        return len(lapsed)


__all__ = ["InMemoryCooldownRepository"]
