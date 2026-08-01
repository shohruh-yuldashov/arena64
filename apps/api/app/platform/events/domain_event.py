"""`DomainEvent` — what every event on this platform is, structurally.

A base class rather than a protocol, and rather than nothing at all. The
alternative that was considered and rejected is "an event is any frozen
dataclass, and the publisher figures it out": that puts the event *type
string* — the one thing the outbox row, the relay's routing and every
consumer's `handles()` agree on — at the call site of each `publish`, where
two producers can spell it differently for one event.

Here the name is a class attribute on the event, so `PlayerBlocked` has
exactly one type string in the entire process, and a consumer that wants to
subscribe imports the class rather than retyping the literal.

## Framework-free, and JSON-shaped

Nothing here imports SQLAlchemy, FastAPI or Pydantic (architecture.md §8).
An event is a fact, and it must be constructible in a unit test with no
database and serialisable into a `jsonb` column with no mapper.

`payload()` returns the **self-contained** body AD-16 requires: everything a
consumer needs to act, with no expectation that the row it describes still
looks the same — or still exists — when it is read. A payload holding only
an id is a payload whose consumer has to go and re-read state that may have
moved on, which is how "eventually consistent" turns into "occasionally
wrong".

The deliberate exception is **state a consumer must not act on stale**:
relationships and privacy. A presence event carries who came online, not who
may be told about it, because the block list at delivery is the only one
that counts (A64-013.7). See `SocialNotificationDispatcher`.

## Versioning

`event_version` is on the row (database.md §10.5) and defaults to `1`. It is
bumped when a payload's *meaning* changes in a way a reader cannot detect —
adding a field is additive and needs no bump, removing or repurposing one
does. The relay never inspects it; consumers do.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID


@dataclass(frozen=True)
class DomainEvent:
    """A fact that has already happened, ready to be made durable.

    **Past tense, always** — `PlayerBlocked`, not `BlockPlayer`. An event is
    not a request; by the time one exists the transaction that caused it is
    either committing or rolling back, and nothing downstream may decline
    it.

    Subclasses set the two class attributes and implement the two members.
    They are deliberately not abstract: `DomainEvent` is never instantiated
    directly, and an `ABCMeta` on a frozen dataclass hierarchy buys a
    runtime error one line earlier than the `NotImplementedError` below, at
    the cost of a metaclass conflict the first time somebody wants a
    `Protocol` alongside it.
    """

    #: The wire name, stored in `outbox.event_type` and matched by every
    #: consumer's `handles()`. Namespaced by owning context — `friends.*`,
    #: `users.*` — so two contexts cannot collide on a bare noun, and so an
    #: operator reading the table can tell who emitted a row.
    event_type: ClassVar[str] = ""

    #: database.md §10.5's `aggregate_type`. What kind of thing this event is
    #: about, for the operator querying the outbox by subject rather than by
    #: type.
    aggregate_type: ClassVar[str] = ""

    event_version: ClassVar[int] = 1

    #: When the fact became true, from the injected clock (AD-07) — never
    #: `datetime.now()`. This is the outbox's ordering key (database.md
    #: §12.5: "publication order must follow causation order"), so a
    #: wall-clock read here would order events by when the *process* noticed
    #: rather than by when they happened.
    occurred_at: datetime

    @property
    def aggregate_id(self) -> UUID:
        """Which instance of `aggregate_type` this is about.

        A property rather than a field because the answer is usually one of
        the ids the subclass already carries, and duplicating it would
        create two places for it to be wrong.
        """
        raise NotImplementedError

    def payload(self) -> dict[str, Any]:
        """The self-contained body, as JSON-serialisable primitives.

        `str` for every `UUID` and ISO-8601 for every instant: the column is
        `jsonb`, and a payload that only round-trips through Python's JSON
        encoder because a custom default was installed is one that stops
        round-tripping the day something else reads the table.
        """
        raise NotImplementedError
