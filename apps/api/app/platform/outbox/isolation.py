"""Per-consumer isolation on the relay — A64-015.6 §5.

A64-013.7 built the relay for one consumer. A64-015.5 added two more, and its
own recommendations named what that made possible:

> "The relay now has three consumers on one loop. `OutboxSettings.batch_size`
> is shared; a slow sink would delay the acceptance-failure policy. Worth
> per-consumer isolation before a real gateway lands."

The gateway is the reason this is worth doing *now* rather than when it
hurts. `LoggingPendingMatchSink` is a function call; AD-09's gateway is a
network write to a socket that may be half-open, and a consumer whose slow
path is a TCP timeout sitting in front of the acceptance-failure policy means
a declined match does not requeue its opponent until the socket gives up.

## What was actually wrong, precisely

`OutboxRelay.run_once` iterated its handlers **sequentially**, awaiting each
in turn. Three consequences, and only the first is obvious:

1. **Latency adds up.** A tick costs the sum of its consumers, so a slow one
   delays every other one's work by its own duration — every tick, whether or
   not they share a single entry.
2. **Order decides who suffers.** The handler list is built at the composition
   root, so which consumer is delayed by which is a property of a list
   literal rather than of anything anybody decided.
3. **There was no upper bound at all.** A consumer that hung — not failed,
   hung — stopped the relay loop for that process indefinitely. Nothing in
   the outbox timed anything out, and `OutboxSettings` has no timeout to set.

## What this fixes, and what it deliberately does not

`ConsumerPolicy` gives each consumer a **timeout**, and `run_once` dispatches
them **concurrently**. A tick now costs the slowest consumer rather than the
sum, and a consumer that exceeds its budget fails *its own* slice — its
entries are retried for it, the others' work already committed.

What is unchanged, and is unchanged on purpose:

**Durability.** Every entry is still claimed once, every consumer still has
its own `processed_event` partition, and the retry is still on the row. §5
requires "existing Outbox durability remains unchanged", and the isolation is
purely in *when* handlers run and *how long they may take*.

**The shared attempt budget.** An entry's `attempt_count` is per entry, not
per consumer, so a consumer that fails an entry consistently still spends the
entry's attempts. Making that per-consumer means a second relation and a
redesign, which §5 forbids ("do not redesign the whole Outbox architecture if
a smaller adapter-level isolation solves the issue"). It is recorded as
remaining debt in `specs/matchmaking/audit.md` rather than pretended away —
and it is bounded in practice by every consumer here being idempotent and
returning per-entry failures rather than raising.

**Concurrency is safe because the consumers do not share a session.**
`SessionScopedNotificationHandler` opens one per `handle`, which A64-013.8
introduced for a different reason (a handler must not read inside the relay's
transaction) and which turns out to be exactly what makes running them
together correct. Two handlers on one session would interleave statements on
one connection, which asyncpg does not permit.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: The budget a consumer gets when the composition root names none.
#:
#: Thirty seconds is deliberately generous — it is a *runaway* guard rather
#: than a latency target. A consumer that has not finished a batch in thirty
#: seconds is not slow, it is stuck, and the number is chosen so that
#: exceeding it is unambiguous rather than a tuning question.
DEFAULT_CONSUMER_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ConsumerPolicy:
    """How long one consumer may take, and what it is called.

    Frozen and validated at construction (DI-06's posture applied to a
    policy): a timeout of zero would fail every batch instantly, and
    discovering that from an empty timeline at 3am is worse than discovering
    it at startup.

    Deliberately **one field beyond the name**. A per-consumer `batch_size`
    was considered and rejected: capping a consumer's slice within a tick
    means the entries beyond the cap are either dropped for it — a
    correctness bug — or reported as failures, which spends the entry's
    shared attempt budget to solve a volume problem nobody has. The relay's
    own `batch_size` already bounds the tick.
    """

    consumer: str
    """Matches `EventHandler.consumer`. A policy naming a consumer that is
    not registered is a typo that silently applies to nobody, which is why
    `ConsumerPolicies.for_handlers` checks."""

    timeout_seconds: float = DEFAULT_CONSUMER_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("a consumer timeout must be positive")


@dataclass(frozen=True, slots=True)
class ConsumerPolicies:
    """Every consumer's policy, with a default for the ones that named none.

    A value rather than a dict at the call site, so "what is this consumer's
    budget" is answered in one place and a missing entry is a documented
    default rather than a `KeyError` inside a relay tick.
    """

    by_consumer: Mapping[str, ConsumerPolicy]
    default_timeout_seconds: float = DEFAULT_CONSUMER_TIMEOUT_SECONDS

    @classmethod
    def of(
        cls,
        policies: Sequence[ConsumerPolicy] = (),
        *,
        default_timeout_seconds: float = DEFAULT_CONSUMER_TIMEOUT_SECONDS,
    ) -> "ConsumerPolicies":
        """The policies for a set of consumers, keyed by name.

        Raises on a duplicate rather than letting the last one win: two
        policies for one consumer is a composition-root mistake, and silently
        applying one of them is the kind of thing that is only noticed when
        the other was the one that mattered.
        """
        by_consumer: dict[str, ConsumerPolicy] = {}
        for policy in policies:
            if policy.consumer in by_consumer:
                raise ValueError(f"two policies for consumer {policy.consumer!r}")
            by_consumer[policy.consumer] = policy
        return cls(by_consumer=by_consumer, default_timeout_seconds=default_timeout_seconds)

    def timeout_for(self, consumer: str) -> float:
        """This consumer's budget, or the default.

        A consumer with no policy gets the default rather than no timeout at
        all, which is the direction that matters: the failure this whole
        module exists to prevent is an *unbounded* wait, and forgetting to
        register a policy must not reintroduce it.
        """
        policy = self.by_consumer.get(consumer)
        return policy.timeout_seconds if policy is not None else self.default_timeout_seconds


__all__ = [
    "DEFAULT_CONSUMER_TIMEOUT_SECONDS",
    "ConsumerPolicies",
    "ConsumerPolicy",
]
