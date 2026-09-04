"""Behavioural events from a browser — analytics.md §37, §38, §39.

**Everything arriving here is untrusted.** The service's whole job is to
decide what a submission may become, and it does that by taking almost
nothing from the request:

    the client supplies    an event name, a property map, an idempotency
                           key, an anonymous id, a session id
    the server decides     the event id, the source, the environment, both
                           timestamps, the subject, and whether the traffic
                           is synthetic

The asymmetry is the design. A client that could set `environment` could
write into production's numbers from a laptop; one that could set `actor_id`
could write into somebody else's history; one that could set `is_synthetic`
could hide its own traffic — or, worse, mark a real player's as test and
delete them from every metric.

## Three refusals, and none of them is a review convention

    a name outside the taxonomy         rejected
    a name the taxonomy owns to the     rejected — `CLIENT_EMITTABLE` is
    **server**                          derived from `Owner`, so this needs
                                        no second list to keep in step
    properties that fail the schema     rejected, including unknown keys

The second is the one that matters. `user_registered`, `match_completed` and
`rating_changed` are facts this platform establishes; accepting one from a
request body would let anybody write Arena64's own history.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from pydantic import ValidationError

from app.config.environment import Environment
from app.core.clock import Clock
from app.modules.analytics.application.ports import AnalyticsEventStore, SubjectDirectory
from app.modules.analytics.domain.event import AnalyticsEvent
from app.modules.analytics.domain.schemas import schema_for
from app.modules.analytics.domain.subject import SubjectKey
from app.modules.analytics.metrics import (
    ANALYTICS_EVENTS_INGESTED,
    ANALYTICS_EVENTS_REJECTED,
    IngestionResult,
    RejectionReason,
)
from app.platform.analytics import CLIENT_EMITTABLE, EventName, Owner, spec_for
from app.platform.metrics import MetricsRecorder

logger = logging.getLogger(__name__)


class EventNotAcceptable(Exception):
    """The submission cannot become an event, and the caller is at fault.

    One exception for all three refusals, carrying a message safe to return.
    Deliberately *not* three: a client learning **which** kind of rejection
    it hit learns which names are server-owned, and an endpoint that answers
    that question is an oracle for the taxonomy.
    """


@dataclass(frozen=True, slots=True)
class ClientEventSubmission:
    """One event as a browser offered it. Nothing here is trusted."""

    name: str
    properties: dict[str, Any]

    #: The client's dedup key. Format-validated and used to derive the
    #: stored id; it grants **no authority** — a client cannot suppress
    #: somebody else's event by guessing one, because the derivation mixes
    #: in the identity the server resolved (§27).
    idempotency_key: UUID

    anonymous_id: UUID
    session_id: UUID | None = None


class ClientEventCollector:
    """Turns submissions into stored events, or refuses them."""

    def __init__(
        self,
        *,
        store: AnalyticsEventStore,
        subjects: SubjectDirectory,
        clock: Clock,
        environment: Environment,
    ) -> None:
        self._store = store
        self._subjects = subjects
        self._clock = clock
        self._environment = environment

    async def collect(
        self,
        submissions: list[ClientEventSubmission],
        *,
        player_id: UUID | None,
        metrics: MetricsRecorder,
    ) -> int:
        """Stores what is acceptable. Returns how many rows were new.

        The **whole batch is refused** if any submission is unacceptable,
        rather than storing the good ones: a partial success would leave a
        client unable to tell which of its events landed, and a client that
        cannot tell will retry all of them.
        """
        subject_key: SubjectKey | None = None
        is_synthetic = False
        if player_id is not None:
            subject_key = await self._subjects.resolve(player_id)
            is_synthetic = await self._is_synthetic(player_id)

        now = self._clock.now()
        events = [
            self._build(
                submission,
                subject_key=subject_key,
                is_synthetic=is_synthetic,
                now=now,
                metrics=metrics,
            )
            for submission in submissions
        ]

        stored = await self._store.append(events)
        metrics.increment(
            ANALYTICS_EVENTS_INGESTED,
            labels={"result": IngestionResult.STORED.value},
            by=stored,
        )
        if (duplicates := len(events) - stored) > 0:
            metrics.increment(
                ANALYTICS_EVENTS_INGESTED,
                labels={"result": IngestionResult.DUPLICATE.value},
                by=duplicates,
            )
        return stored

    async def _is_synthetic(self, player_id: UUID) -> bool:
        """Read from the subject directory, never from the request (§46)."""
        return await self._subjects.is_synthetic(player_id)

    def _build(
        self,
        submission: ClientEventSubmission,
        *,
        subject_key: SubjectKey | None,
        is_synthetic: bool,
        now: datetime,
        metrics: MetricsRecorder,
    ) -> AnalyticsEvent:
        try:
            name = EventName(submission.name)
        except ValueError as error:
            metrics.increment(
                ANALYTICS_EVENTS_REJECTED,
                labels={"reason": RejectionReason.NOT_CLIENT_EMITTABLE.value},
            )
            raise EventNotAcceptable("that event is not one this endpoint accepts") from error

        if name not in CLIENT_EMITTABLE:
            metrics.increment(
                ANALYTICS_EVENTS_REJECTED,
                labels={"reason": RejectionReason.NOT_CLIENT_EMITTABLE.value},
            )
            # Logged, because a client trying to submit a server event is
            # either a bug in this repository's own tracker or somebody
            # probing the boundary. Both are worth seeing.
            logger.warning("analytics_client_event_refused", extra={"event_name": name.value})
            raise EventNotAcceptable("that event is not one this endpoint accepts")

        try:
            validated = schema_for(name).model_validate(submission.properties)
        except ValidationError as error:
            metrics.increment(
                ANALYTICS_EVENTS_REJECTED,
                labels={"reason": RejectionReason.INVALID_PROPERTIES.value},
            )
            raise EventNotAcceptable("those event properties are not valid") from error

        return AnalyticsEvent(
            # Derived from the client's key **and** the identity the server
            # resolved, so a retry of the same submission by the same
            # browser collapses and a different browser's identical key
            # cannot collide with it.
            event_id=_client_event_id(submission, subject_key),
            event_name=name,
            event_version=spec_for(name).version,
            # Both the server's clock. A browser's `occurred_at` is not
            # accepted at all (§28) — a value nobody can trust is a value a
            # metric must not bucket by, and storing it "for context"
            # invites exactly that.
            occurred_at=now,
            received_at=now,
            source=Owner.FRONTEND.value,
            environment=self._environment,
            subject_key=subject_key,
            anonymous_id=submission.anonymous_id,
            session_id=submission.session_id,
            is_synthetic=is_synthetic,
            properties=validated.model_dump(mode="json", exclude_none=True),
        )


def _client_event_id(submission: ClientEventSubmission, subject_key: SubjectKey | None) -> UUID:
    """A stable id for one submission by one identity.

    `uuid5` over the client's key and the browser's anonymous id: the same
    submission retried after a timeout produces the same id and conflicts,
    which is what makes a lost response safe to retry. It is **not** a
    capability — two browsers sending the same key produce different ids,
    so nothing can be suppressed by guessing.
    """
    namespace = UUID("2b8a5c31-7d94-5f0e-a1c6-3e8b4f2d9a70")
    scope = str(subject_key) if subject_key is not None else str(submission.anonymous_id)
    return uuid5(namespace, f"{scope}:{submission.idempotency_key}")
