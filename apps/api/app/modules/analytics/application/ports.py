"""What the analytics services hold — AD-06, ports beside their consumers.

Four, split by capability rather than by convenience:

    AnalyticsEventStore   append events, idempotently. The only write path
    SubjectDirectory      resolve a player to their opaque analytics subject
    SubjectEraser         destroy that link, irreversibly (§51)
    RetentionPruner       delete raw events past the horizon, in batches

`AnalyticsEventStore.append` returns how many rows were **new**, not how
many were offered. That difference is the deduplication working, and it is
what the operational counter reports — a consumer that could not tell the
two apart would report a healthy throughput while storing nothing.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.analytics.domain.event import AnalyticsEvent
from app.modules.analytics.domain.subject import SubjectKey


class AnalyticsEventStore(Protocol):
    async def append(self, events: Sequence[AnalyticsEvent]) -> int:
        """Stores events, ignoring those already present.

        `ON CONFLICT DO NOTHING` on the primary key, so the same event
        offered twice — by a relay redelivery, by two workers racing, by a
        crash between the write and the ledger — is stored once.

        Returns the number of rows actually inserted.
        """
        ...


class SubjectDirectory(Protocol):
    async def resolve(self, player_id: UUID) -> SubjectKey:
        """This player's analytics subject, creating one if there is none.

        Idempotent under concurrency: two requests for a player with no
        subject must produce one key, not two, or the same person appears
        as two subjects and every per-person metric splits them.
        """
        ...

    async def lookup(self, player_id: UUID) -> SubjectKey | None:
        """This player's subject, or `None` if they have never had one.

        Distinct from `resolve` because erasure and read paths must not
        create a subject as a side effect of asking about one.
        """
        ...

    async def mark_synthetic(self, player_id: UUID, *, is_synthetic: bool) -> None:
        """Flags an account's events as test traffic (§46).

        Called by the operator command that seeds e2e accounts, never by a
        request: the flag is what excludes an account from every product
        metric, so a client able to set it could delete a real player from
        the numbers or hide its own traffic in them.
        """
        ...

    async def is_synthetic(self, player_id: UUID) -> bool:
        """Whether this account's events are test traffic.

        Read on the collector path so the stored row carries the answer,
        rather than every query joining to a table erasure can delete.
        """
        ...


class SubjectEraser(Protocol):
    async def erase(self, player_id: UUID) -> bool:
        """Destroys the link between a player and their analytics history.

        Deletes the subject row. The events keep their `subject_key`, which
        is a random value with no derivation from anything, so after this
        there is no function from the player to their rows and none from
        the rows back to the player.

        Returns whether a link existed. Idempotent: erasing twice is not an
        error, because an erasure that failed on the second attempt would
        make the *retry* of a deletion request fail.
        """
        ...


class RetentionPruner(Protocol):
    async def delete_older_than(self, cutoff: datetime, *, limit: int) -> int:
        """Deletes at most `limit` events older than `cutoff`.

        Bounded on purpose: an unbounded `DELETE` over a table with a year
        of events is one long transaction holding one long lock, and the
        first symptom is ingestion blocking behind it.
        """
        ...
