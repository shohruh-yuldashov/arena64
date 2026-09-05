"""The analytics pipeline's contracts — A64-027.2.

Four things must hold, and each fails silently if it stops holding:

    the taxonomy is total          an event with no schema is one whose
                                   properties nothing validates
    the vocabularies match         analytics declares its own enums so a
                                   domain rename cannot silently change
                                   stored values; the price is that they
                                   must be asserted equal
    a client cannot write history  the collector's refusals
    privacy is structural          no schema declares a denied field, and
                                   no property type can hold prose

The security half lives in `TestTheCollectorRefuses`. Those are the tests
the mutation checks in A64-027.2 §71 target.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.config.environment import Environment
from app.modules.analytics.application.services.collector import (
    ClientEventCollector,
    ClientEventSubmission,
    EventNotAcceptable,
)
from app.modules.analytics.application.services.projections import (
    ProjectionError,
    project,
)
from app.modules.analytics.domain import properties as ap
from app.modules.analytics.domain.event import AnalyticsEvent, seat_event_id
from app.modules.analytics.domain.privacy import denied_fields_across_taxonomy
from app.modules.analytics.domain.schemas import SCHEMAS
from app.modules.analytics.domain.subject import SubjectKey
from app.modules.game.domain.result import MatchOutcome as DomainOutcome
from app.modules.game.domain.result import TerminationReason as DomainTermination
from app.modules.game.domain.variants import MatchOrigin as DomainOrigin
from app.modules.game.domain.variants import ProductVariant as DomainVariant
from app.modules.game.public.metrics import MatchOutcome as DomainOfferOutcome
from app.modules.matchmaking.domain.queue_pool import QueueType as DomainQueueType
from app.modules.rating.domain.keys import SpeedClass as DomainSpeedClass
from app.modules.tournament.domain.tournament import TournamentFormat as DomainFormat
from app.modules.tournament.domain.tournament import TournamentStatus as DomainStatus
from app.platform.analytics import CLIENT_EMITTABLE, EventName
from app.platform.metrics import NullMetrics
from app.platform.outbox import OutboxEntry

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class _RecordingStore:
    """Remembers what it was asked to store, and deduplicates by id."""

    def __init__(self) -> None:
        self.events: dict[UUID, AnalyticsEvent] = {}

    async def append(self, events):  # type: ignore[no-untyped-def]
        new = 0
        for event in events:
            if event.event_id not in self.events:
                self.events[event.event_id] = event
                new += 1
        return new


class _FixedSubjects:
    def __init__(self, key: SubjectKey | None = None, *, synthetic: bool = False) -> None:
        self.key = key or SubjectKey(uuid4())
        self.synthetic = synthetic

    async def resolve(self, player_id):  # type: ignore[no-untyped-def]
        return self.key

    async def lookup(self, player_id):  # type: ignore[no-untyped-def]
        return self.key

    async def is_synthetic(self, player_id):  # type: ignore[no-untyped-def]
        return self.synthetic

    async def mark_synthetic(self, player_id, *, is_synthetic):  # type: ignore[no-untyped-def]
        self.synthetic = is_synthetic


class _FixedClock:
    def now(self) -> datetime:
        return NOW


def _collector(store: _RecordingStore, subjects: _FixedSubjects) -> ClientEventCollector:
    return ClientEventCollector(
        store=store, subjects=subjects, clock=_FixedClock(), environment=Environment.TEST
    )


def _submission(name: str, **properties: object) -> ClientEventSubmission:
    return ClientEventSubmission(
        name=name,
        properties=dict(properties),
        idempotency_key=uuid4(),
        anonymous_id=uuid4(),
    )


class TestTheTaxonomyIsTotal:
    def test_every_event_has_a_schema(self) -> None:
        """An event with no schema is one whose properties nothing checks —
        so the `jsonb` column would hold whatever a caller sent."""
        assert set(SCHEMAS) == set(EventName)


class TestTheVocabulariesMatch:
    """Analytics declares its own enums (§7's versioning argument).

    The price of decoupling is that nothing keeps them equal, so these do.
    A domain rename now fails a test that names *analytics*, which is where
    the decision about an `event_version` bump belongs.
    """

    @pytest.mark.parametrize(
        ("analytics_enum", "domain_enum"),
        [
            (ap.Variant, DomainVariant),
            (ap.SpeedClass, DomainSpeedClass),
            (ap.Outcome, DomainOutcome),
            (ap.OfferResolution, DomainOfferOutcome),
            (ap.TerminationReason, DomainTermination),
            (ap.MatchOrigin, DomainOrigin),
            (ap.QueueType, DomainQueueType),
            (ap.TournamentFormat, DomainFormat),
            (ap.TournamentStatus, DomainStatus),
        ],
    )
    def test_analytics_mirrors_the_domain_exactly(self, analytics_enum, domain_enum) -> None:  # type: ignore[no-untyped-def]
        assert {member.value for member in analytics_enum} == {
            member.value for member in domain_enum
        }

    def test_every_termination_reason_is_classifiable(self) -> None:
        """§32's completion rate branches on all eleven. A member missing
        here would silently move games out of a denominator."""
        assert len(ap.TerminationReason) == 11
        assert ap.TerminationReason.ABORT.value == "abort"


class TestTheCollectorRefuses:
    """The security boundary. **MUTATION A and B target these.**"""

    async def test_a_client_cannot_emit_match_completed(self) -> None:
        store, subjects = _RecordingStore(), _FixedSubjects()
        with pytest.raises(EventNotAcceptable):
            await _collector(store, subjects).collect(
                [_submission("match_completed", match_id=str(uuid4()))],
                player_id=uuid4(),
                metrics=NullMetrics(),
            )
        assert store.events == {}

    @pytest.mark.parametrize(
        "name",
        [
            "user_registered",
            "email_verified",
            "queue_joined",
            "match_found",
            "match_started",
            "rating_changed",
            "tournament_entered",
            "tournament_completed",
            "challenge_sent",
            "friendship_created",
        ],
    )
    async def test_a_client_cannot_emit_any_server_event(self, name: str) -> None:
        """Named one by one, because the list is the thing being protected."""
        store, subjects = _RecordingStore(), _FixedSubjects()
        with pytest.raises(EventNotAcceptable):
            await _collector(store, subjects).collect(
                [_submission(name)], player_id=uuid4(), metrics=NullMetrics()
            )
        assert store.events == {}

    async def test_an_unknown_name_is_refused(self) -> None:
        store, subjects = _RecordingStore(), _FixedSubjects()
        with pytest.raises(EventNotAcceptable):
            await _collector(store, subjects).collect(
                [_submission("definitely_not_an_event")], player_id=None, metrics=NullMetrics()
            )

    async def test_an_unknown_property_is_refused(self) -> None:
        """`extra="forbid"`, not "ignore". A silently dropped field and a
        `202` teaches a client to keep sending it."""
        store, subjects = _RecordingStore(), _FixedSubjects()
        with pytest.raises(EventNotAcceptable):
            await _collector(store, subjects).collect(
                [_submission("landing_viewed", email="nobody@example.com")],
                player_id=None,
                metrics=NullMetrics(),
            )
        assert store.events == {}

    async def test_a_denied_property_cannot_reach_the_store(self) -> None:
        for field in ("email", "username", "display_name", "ip_address", "user_agent", "bio"):
            store, subjects = _RecordingStore(), _FixedSubjects()
            with pytest.raises(EventNotAcceptable):
                await _collector(store, subjects).collect(
                    [_submission("share_clicked", **{field: "x"})],
                    player_id=None,
                    metrics=NullMetrics(),
                )

    async def test_the_whole_batch_is_refused_when_one_event_is_bad(self) -> None:
        """A partial success leaves a client unable to tell which of its
        events landed, and a client that cannot tell retries all of them."""
        store, subjects = _RecordingStore(), _FixedSubjects()
        good = ClientEventSubmission(
            name="landing_viewed",
            properties={},
            idempotency_key=uuid4(),
            anonymous_id=uuid4(),
        )
        with pytest.raises(EventNotAcceptable):
            await _collector(store, subjects).collect(
                [good, _submission("user_registered")], player_id=None, metrics=NullMetrics()
            )
        assert store.events == {}


class TestTheServerOwnsTheEnvelope:
    """**MUTATION B targets these.**"""

    async def test_the_actor_comes_from_the_session(self) -> None:
        """A submission has no field for it, so this asserts the positive
        half: what the caller passed as the principal is what is stored."""
        store = _RecordingStore()
        key = SubjectKey(uuid4())
        await _collector(store, _FixedSubjects(key)).collect(
            [_submission("share_clicked", surface="tournament", mechanism="clipboard")],
            player_id=uuid4(),
            metrics=NullMetrics(),
        )
        stored = next(iter(store.events.values()))
        assert stored.subject_key == key

    async def test_an_anonymous_submission_has_no_subject(self) -> None:
        store = _RecordingStore()
        await _collector(store, _FixedSubjects()).collect(
            [_submission("landing_viewed")], player_id=None, metrics=NullMetrics()
        )
        stored = next(iter(store.events.values()))
        assert stored.subject_key is None
        assert stored.anonymous_id is not None

    async def test_the_environment_is_the_process_not_the_request(self) -> None:
        store = _RecordingStore()
        await _collector(store, _FixedSubjects()).collect(
            [_submission("landing_viewed")], player_id=None, metrics=NullMetrics()
        )
        assert next(iter(store.events.values())).environment is Environment.TEST

    async def test_synthetic_comes_from_the_account(self) -> None:
        """A client cannot mark itself as test traffic, or a real player as
        test traffic — either would remove somebody from every metric."""
        store = _RecordingStore()
        await _collector(store, _FixedSubjects(synthetic=True)).collect(
            [_submission("landing_viewed")], player_id=uuid4(), metrics=NullMetrics()
        )
        assert next(iter(store.events.values())).is_synthetic is True

    async def test_the_timestamps_are_the_servers(self) -> None:
        store = _RecordingStore()
        await _collector(store, _FixedSubjects()).collect(
            [_submission("landing_viewed")], player_id=None, metrics=NullMetrics()
        )
        stored = next(iter(store.events.values()))
        assert stored.occurred_at == NOW
        assert stored.received_at == NOW

    async def test_the_source_is_frontend(self) -> None:
        store = _RecordingStore()
        await _collector(store, _FixedSubjects()).collect(
            [_submission("landing_viewed")], player_id=None, metrics=NullMetrics()
        )
        assert next(iter(store.events.values())).source == "frontend"


class TestClientDeduplication:
    """**MUTATION C targets this.**"""

    async def test_the_same_submission_retried_is_stored_once(self) -> None:
        store, subjects = _RecordingStore(), _FixedSubjects()
        collector = _collector(store, subjects)
        submission = _submission("landing_viewed")

        first = await collector.collect([submission], player_id=None, metrics=NullMetrics())
        second = await collector.collect([submission], player_id=None, metrics=NullMetrics())

        assert (first, second) == (1, 0)
        assert len(store.events) == 1

    async def test_two_browsers_reusing_one_key_do_not_collide(self) -> None:
        """The key is a dedup identity, never a capability: a client cannot
        suppress somebody else's event by guessing theirs."""
        store, subjects = _RecordingStore(), _FixedSubjects()
        collector = _collector(store, subjects)
        shared = uuid4()
        one = ClientEventSubmission(
            name="landing_viewed", properties={}, idempotency_key=shared, anonymous_id=uuid4()
        )
        two = ClientEventSubmission(
            name="landing_viewed", properties={}, idempotency_key=shared, anonymous_id=uuid4()
        )

        await collector.collect([one], player_id=None, metrics=NullMetrics())
        await collector.collect([two], player_id=None, metrics=NullMetrics())

        assert len(store.events) == 2


class TestPrivacyIsStructural:
    def test_no_event_schema_declares_a_denied_field(self) -> None:
        """The check that catches the field added in good faith — a
        `username` so a funnel can be read during an incident."""
        assert denied_fields_across_taxonomy() == {}

    def test_no_property_type_can_hold_prose(self) -> None:
        """Key-name scanning is not the control; typing is.

        Every declared field is an enum, a bool, a number, or one of the
        few bounded strings — an id or a `utm_*` value. Nothing accepts
        free text, which is what makes it structurally impossible to put an
        address in a property called `label`.
        """
        allowed_strings = {"match_id", "tournament_id", "utm_source", "utm_medium", "utm_campaign"}
        for name, schema in SCHEMAS.items():
            for field, info in schema.model_fields.items():
                annotation = str(info.annotation)
                if "str" in annotation and field not in allowed_strings:
                    # An enum's annotation mentions its own class, not `str`
                    # — unless it is a bare string, which is what this
                    # catches.
                    pytest.fail(f"{name.value}.{field} is an unbounded string")


class TestProjectionsReadOnlyThePayload:
    def _entry(self, event_type: str, payload: dict[str, object], version: int = 1) -> OutboxEntry:
        return OutboxEntry(
            id=uuid4(),
            aggregate_type="player",
            aggregate_id=uuid4(),
            event_type=event_type,
            event_version=version,
            payload=payload,
            occurred_at=NOW,
        )

    def test_an_untracked_event_projects_to_nothing(self) -> None:
        """§17: the relay hands every consumer every entry, and most belong
        to somebody else. That is not an error."""
        assert project(self._entry("game.move_applied", {})) == ()

    def test_an_unsupported_version_is_an_error_not_a_guess(self) -> None:
        """§18: interpreting a v2 payload with v1's reader is how a silent
        wrong number gets stored."""
        with pytest.raises(ProjectionError, match="version"):
            project(self._entry("users.registered", {"user_id": str(uuid4())}, version=2))

    def test_a_missing_field_is_an_error(self) -> None:
        with pytest.raises(ProjectionError):
            project(self._entry("users.registered", {}))

    def test_a_pairing_becomes_one_event_per_seat(self) -> None:
        light, dark = uuid4(), uuid4()
        entry = self._entry(
            "matchmaking.players_paired",
            {
                "match_id": str(uuid4()),
                "variant": "russian_8x8",
                "queue_type": "ranked",
                "light_player_id": str(light),
                "dark_player_id": str(dark),
                "waited_for_seconds": 4.25,
            },
        )

        events = project(entry)

        assert len(events) == 2
        assert {event.player_id for event in events} == {light, dark}
        # Deterministic and distinct, which is what lets both rows share one
        # outbox id and still be deduplicated by primary key.
        assert events[0].event_id == seat_event_id(entry.id, "light")
        assert events[0].event_id != events[1].event_id
        assert all(event.properties["waited_ms"] == 4250 for event in events)

    def test_a_tournament_name_is_never_projected(self) -> None:
        """§14: an unbounded string that answers no question
        `tournament_id` does not."""
        entry = self._entry(
            "tournament.player_registered",
            {"player_id": str(uuid4()), "name": "Sunday Open"},
        )

        properties = project(entry)[0].properties

        assert "name" not in properties
        assert "Sunday Open" not in str(properties)

    def test_a_tournament_winner_is_never_projected(self) -> None:
        """Available in the payload, read by no metric, and a person."""
        entry = self._entry("tournament.completed", {"winner_id": str(uuid4())})

        properties = project(entry)[0].properties

        assert "winner_id" not in properties


class TestTheIdentityInvariant:
    def test_a_match_level_event_may_not_carry_an_identity(self) -> None:
        """The failure that produces a wrong number rather than a small
        one: one game counted for one of its two seats."""
        with pytest.raises(ValueError, match="entity-level"):
            AnalyticsEvent(
                event_id=uuid4(),
                event_name=EventName.MATCH_COMPLETED,
                event_version=1,
                occurred_at=NOW,
                received_at=NOW,
                source="backend",
                environment=Environment.TEST,
                subject_key=SubjectKey(uuid4()),
            )

    def test_a_persons_event_needs_a_subject(self) -> None:
        with pytest.raises(ValueError, match="subject"):
            AnalyticsEvent(
                event_id=uuid4(),
                event_name=EventName.USER_REGISTERED,
                event_version=1,
                occurred_at=NOW,
                received_at=NOW,
                source="backend",
                environment=Environment.TEST,
            )

    def test_the_client_events_are_exactly_the_anonymous_ones(self) -> None:
        from app.platform.analytics import Identity, spec_for

        assert all(spec_for(name).identity is Identity.ANONYMOUS for name in CLIENT_EMITTABLE)
