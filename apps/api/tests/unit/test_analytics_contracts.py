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
from typing import Final
from uuid import UUID, uuid4

import pytest

from app.config.environment import Environment
from app.modules.analytics.application.services.collector import (
    ClientEventCollector,
    ClientEventSubmission,
    EventNotAcceptable,
)
from app.modules.analytics.application.services.projections import (
    PROJECTIONS,
    ProjectionError,
    finalise,
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
from app.modules.game.domain.variants import ProductVariant
from app.modules.game.domain.variants import ProductVariant as DomainVariant
from app.modules.game.public.metrics import MatchOutcome as DomainOfferOutcome
from app.modules.matchmaking.domain.challenge_events import FriendChallengeCreated
from app.modules.matchmaking.domain.events import QueueTicketEnqueued
from app.modules.matchmaking.domain.queue_pool import QueueType, Region
from app.modules.matchmaking.domain.queue_pool import QueueType as DomainQueueType
from app.modules.rating.domain.keys import SpeedClass as DomainSpeedClass
from app.modules.reference.domain.time_control import TimeControlId
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

    def test_a_pairing_never_produces_a_queue_abandonment(self) -> None:
        """A64-027.5 §13, asserted structurally.

        "Match found, therefore the queue entry was removed, therefore
        abandoned" is the way M7b goes quietly wrong. It cannot happen —
        the pairing service publishes `PlayersPaired` and no ticket event —
        and this pins the projection so a future edit cannot reintroduce
        it. A mutation check found that the M7b query tests could not:
        they build their own fixtures and never run a projection.
        """
        entry = self._entry(
            "matchmaking.players_paired",
            {
                "match_id": str(uuid4()),
                "variant": "russian_8x8",
                "queue_type": "ranked",
                "light_player_id": str(uuid4()),
                "dark_player_id": str(uuid4()),
                "waited_for_seconds": 2.0,
            },
        )

        produced = {event.name for event in project(entry)}

        assert produced == {EventName.MATCH_FOUND}
        assert EventName.QUEUE_LEFT not in produced

    def test_a_partial_acceptance_is_not_an_offer_resolution(self) -> None:
        """`match_accepted_by_player` is the state where one side has
        answered and the other has not. Projecting it would resolve an
        offer twice — once half-resolved — and M9's denominator would grow
        without its numerator."""
        assert project(self._entry("game.match_accepted_by_player", {})) == ()

    def test_an_activation_yields_two_seats_and_one_offer_resolution(self) -> None:
        """One outbox row, three analytics rows at two different grains:
        the seats are per player and the resolution is per match."""
        entry = self._entry(
            "game.match_activated",
            {
                "match_id": str(uuid4()),
                "variant": "russian_8x8",
                "rated": True,
                "light_player_id": str(uuid4()),
                "dark_player_id": str(uuid4()),
            },
        )

        produced = project(entry)

        assert [event.name for event in produced].count(EventName.MATCH_STARTED) == 2
        resolutions = [e for e in produced if e.name is EventName.MATCH_OFFER_RESOLVED]
        assert len(resolutions) == 1
        assert resolutions[0].properties["resolution"] == "both_accepted"
        assert resolutions[0].player_id is None
        # Three distinct ids from one outbox row, so a redelivery conflicts
        # on each rather than doubling any of them.
        assert len({event.event_id for event in produced}) == 3

    def test_a_negative_queue_wait_is_refused_rather_than_clamped(self) -> None:
        """§50. Clamping to zero would put an impossible value into a
        distribution as a fast pairing."""
        with pytest.raises(ProjectionError, match="negative"):
            project(
                self._entry(
                    "matchmaking.queue_ticket_cancelled",
                    {
                        "player_id": str(uuid4()),
                        "variant": "russian_8x8",
                        "queue_type": "ranked",
                        "waited_for_seconds": -1.0,
                    },
                )
            )

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


#: Source types this file drives end to end — event in, `finalise` out.
_SCHEMA_SATISFACTION_TESTED: Final = frozenset(
    {
        "matchmaking.queue_ticket_enqueued",
        "matchmaking.friend_challenge_created",
    }
)

#: Source types not yet driven end to end. Declared rather than omitted: the
#: two defects this property exists for were each invisible because nobody
#: could see which projections had been checked and which had not. A name
#: here is a debt with an owner, and a projection in neither set fails the
#: guard below until somebody decides which it is.
_SCHEMA_SATISFACTION_DEFERRED: Final = frozenset(
    {
        "friends.friend_request_accepted",
        "friends.friend_request_sent",
        "game.match_acceptance_expired",
        "game.match_activated",
        "game.match_completed",
        "game.match_declined",
        "matchmaking.friend_challenge_accepted",
        "matchmaking.friend_challenge_cancelled",
        "matchmaking.friend_challenge_declined",
        "matchmaking.friend_challenge_expired",
        "matchmaking.players_paired",
        "matchmaking.queue_ticket_cancelled",
        "matchmaking.queue_ticket_expired",
        "rating.updated",
        "tournament.completed",
        "tournament.player_registered",
        "users.email_verified",
        "users.registered",
    }
)

_SCHEMA_SATISFACTION_COVERED: Final = _SCHEMA_SATISFACTION_TESTED | _SCHEMA_SATISFACTION_DEFERRED


class TestAProjectionSatisfiesItsOwnSchema:
    """The half of the contract that was never checked — A64-028.5A §25.

    `finalise` validates a projection's output against the schema, which is
    the right place for it: a projection is repository code and can be
    wrong. What nothing checked was whether a projection *could* satisfy
    its schema at all, and `queue_joined` could not — `QueueJoined` required
    `speed_class` and the ticket event has never carried a time control to
    derive one from.

    The cost was not a warning. Every queue join in every environment
    produced an outbox entry that failed validation, retried five times and
    was abandoned: the third stage of funnel F-B was empty, permanently,
    and the poisoned rows accumulated. A load run found 1,850 of them.
    """

    def test_a_queue_ticket_projects_into_a_valid_event(self) -> None:
        entry = OutboxEntry.of(
            QueueTicketEnqueued(
                ticket_id=uuid4(),
                player_id=uuid4(),
                queue_type=QueueType.CASUAL,
                variant=ProductVariant.RUSSIAN_8X8,
                region=Region.GLOBAL,
                rating_snapshot=1500,
                expires_at=datetime(2026, 9, 5, tzinfo=UTC),
                occurred_at=datetime(2026, 9, 5, tzinfo=UTC),
            )
        )

        (pending,) = project(entry)

        # The assertion is that this does not raise: `finalise` is where the
        # schema runs, and where every one of those 1,850 entries died.
        sealed = finalise(
            pending,
            subject_key=SubjectKey(uuid4()),
            occurred_at=datetime(2026, 9, 5, tzinfo=UTC),
            received_at=datetime(2026, 9, 5, tzinfo=UTC),
            environment=Environment.TEST,
            is_synthetic=False,
            source_event_id=entry.id,
        )

        assert sealed.event_name is EventName.QUEUE_JOINED
        # Absent rather than null: the field is owed additively by
        # matchmaking, and an explicit `None` in the store would read as
        # "measured, and unknown".
        assert "speed_class" not in sealed.properties

    #: The payload production actually produced, from the worker's own log:
    #: `{'variant': 'russian_8x8', 'rated': False}` reaching a schema that
    #: required `speed_class`.
    @pytest.mark.parametrize("rated", [False, True])
    @pytest.mark.parametrize(
        "time_control_id",
        [
            TimeControlId.BULLET_1_0,
            TimeControlId.BLITZ_3_2,
            TimeControlId.RAPID_10_0,
            TimeControlId.CLASSICAL_30_0,
        ],
    )
    def test_a_friend_challenge_projects_into_a_valid_event(
        self, time_control_id: TimeControlId, rated: bool
    ) -> None:
        """The production failure, reproduced — A64-030.4B.1 (B-3).

        Before the fix this raised `ValidationError: speed_class Field
        required`, which is what abandoned every friend challenge the
        platform has ever seen. Every offered time control is exercised
        because the projection must not become sensitive to one.
        """
        entry = OutboxEntry.of(
            FriendChallengeCreated(
                challenge_id=uuid4(),
                challenger_id=uuid4(),
                recipient_id=uuid4(),
                time_control_id=time_control_id,
                variant=ProductVariant.RUSSIAN_8X8,
                rated=rated,
                expires_at=datetime(2026, 9, 6, tzinfo=UTC),
                occurred_at=datetime(2026, 9, 6, tzinfo=UTC),
            )
        )

        (pending,) = project(entry)

        sealed = finalise(
            pending,
            subject_key=SubjectKey(uuid4()),
            occurred_at=datetime(2026, 9, 6, tzinfo=UTC),
            received_at=datetime(2026, 9, 6, tzinfo=UTC),
            environment=Environment.TEST,
            is_synthetic=False,
            source_event_id=entry.id,
        )

        assert sealed.event_name is EventName.CHALLENGE_SENT
        assert sealed.properties["variant"] == ProductVariant.RUSSIAN_8X8.value
        assert sealed.properties["rated"] is rated
        # Absent rather than null, for the reason the queue join gives above.
        assert "speed_class" not in sealed.properties

    def test_the_exact_production_payload_is_accepted(self) -> None:
        """The literal properties the abandoned entries carried.

        Asserted against the schema directly as well as through the
        projection, so the contract holds even if the projection later
        learns to send more.
        """
        schema = SCHEMAS[EventName.CHALLENGE_SENT]

        validated = schema.model_validate({"variant": "russian_8x8", "rated": False})

        assert validated.model_dump(exclude_none=True) == {
            "variant": "russian_8x8",
            "rated": False,
        }

    def test_every_projection_is_covered_by_a_schema_satisfaction_test(self) -> None:
        """The guard that would have caught this one.

        `QueueJoined` was fixed by P1-11 and `ChallengeSent` — the same
        mismatch, written in the same change — survived another two epics,
        because the regression that closed P1-11 asserted the property for
        one projection rather than for projections. A source type that
        reaches this table without a case below is a projection nothing has
        ever validated end to end, which is precisely the state both defects
        were found in.
        """
        untested = set(PROJECTIONS) - _SCHEMA_SATISFACTION_COVERED
        assert not untested, (
            "these projections have no test proving they can satisfy their own schema: "
            f"{sorted(untested)}. Add one to TestAProjectionSatisfiesItsOwnSchema, or "
            "record the omission here deliberately."
        )
