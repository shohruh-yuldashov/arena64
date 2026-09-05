"""Acquisition and activation over real PostgreSQL — A64-027.3.

The arithmetic is unit-tested. This proves the **SQL**, and every assertion
here would pass against an in-memory implementation while the real query
was wrong:

    unique subjects            twenty queue joins is one person, and only a
                               `COUNT(DISTINCT …)` over the right column
                               knows that
    strict nesting             a later stage counting somebody who skipped
                               an earlier one looks entirely plausible
    the activation join        a completion carries no player; only the
                               join to `match_started` supplies one
    the filters                environment and synthetic exclusion are two
                               `WHERE` clauses, and a missing one is
                               invisible until production numbers are wrong

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.environment import Environment
from app.modules.analytics.application.read_models.funnels import Coverage
from app.modules.analytics.application.services.funnels import FunnelService
from app.modules.analytics.domain.event import AnalyticsEvent
from app.modules.analytics.domain.subject import SubjectKey
from app.modules.analytics.infrastructure.repositories.analytics_repository import (
    SqlAlchemyAnalyticsEventStore,
)
from app.modules.analytics.infrastructure.repositories.funnel_repository import (
    SqlAlchemyFunnelReader,
)
from app.platform.analytics import EventName

#: A cohort day well inside retention, and a fixed clock so maturity is
#: deterministic — a wall clock would make these pass for a year and then
#: start failing at a boundary nobody changed.
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
COHORT_DAY = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
PRODUCTION = Environment.PRODUCTION.value


class _FrozenClock:
    def now(self) -> datetime:
        return NOW


class Journey:
    """One synthetic player's path, written as events.

    A builder rather than raw dicts, because a funnel fixture is a sequence
    of instants and getting one of them wrong silently changes what the
    test proves.
    """

    def __init__(
        self,
        *,
        registered_at: datetime = COHORT_DAY,
        environment: Environment = Environment.PRODUCTION,
        synthetic: bool = False,
    ) -> None:
        self.subject = SubjectKey(uuid4())
        self.environment = environment
        self.synthetic = synthetic
        self.events: list[AnalyticsEvent] = []
        self._add(EventName.USER_REGISTERED, registered_at, {})
        self.registered_at = registered_at

    def _add(
        self,
        name: EventName,
        at: datetime,
        properties: dict[str, object],
        *,
        subject: SubjectKey | None = None,
        entity: bool = False,
    ) -> AnalyticsEvent:
        event = AnalyticsEvent(
            event_id=uuid4(),
            event_name=name,
            event_version=1,
            occurred_at=at,
            received_at=at,
            source="backend",
            environment=self.environment,
            subject_key=None if entity else (subject or self.subject),
            is_synthetic=self.synthetic,
            properties=properties,
        )
        self.events.append(event)
        return event

    def verified(self, *, after: timedelta = timedelta(hours=1)) -> "Journey":
        self._add(
            EventName.EMAIL_VERIFIED,
            self.registered_at + after,
            {"hours_since_registration": int(after.total_seconds() // 3600)},
        )
        return self

    def queued(self, *, times: int = 1, after: timedelta = timedelta(hours=2)) -> "Journey":
        for index in range(times):
            self._add(
                EventName.QUEUE_JOINED,
                self.registered_at + after + timedelta(minutes=index),
                {"variant": "russian_8x8", "queue_type": "ranked", "rated": True},
            )
        return self

    def played(
        self,
        *,
        termination: str = "resignation",
        after: timedelta = timedelta(hours=3),
        opponent: "Journey | None" = None,
    ) -> "Journey":
        """A match this player started and that then ended.

        Two rows per seat for the start and **one** entity-level row for the
        completion, which is the shape A64-027.1 §18 froze and the reason
        activation has to be a join.
        """
        match_id = str(uuid4())
        started_at = self.registered_at + after
        self._add(
            EventName.MATCH_STARTED,
            started_at,
            {"match_id": match_id, "variant": "russian_8x8", "rated": True},
        )
        if opponent is not None:
            opponent._add(
                EventName.MATCH_STARTED,
                started_at,
                {"match_id": match_id, "variant": "russian_8x8", "rated": True},
            )
        self._add(
            EventName.MATCH_COMPLETED,
            started_at + timedelta(minutes=20),
            {
                "match_id": match_id,
                "variant": "russian_8x8",
                "rated": True,
                "outcome": "win",
                "termination_reason": termination,
                "ply_count": 42,
                "origin": "queue",
            },
            entity=True,
        )
        return self

    def all_events(self) -> list[AnalyticsEvent]:
        return self.events


async def store(session: AsyncSession, *journeys: Journey) -> None:
    events = [event for journey in journeys for event in journey.all_events()]
    await SqlAlchemyAnalyticsEventStore(session).append(events)


def service(session: AsyncSession) -> FunnelService:
    return FunnelService(reader=SqlAlchemyFunnelReader(session), clock=_FrozenClock())


async def activation(session: AsyncSession, **overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "environment": PRODUCTION,
        "since": COHORT_DAY.date(),
        "until": COHORT_DAY.date(),
    }
    arguments.update(overrides)
    return await service(session).activation(**arguments)  # type: ignore[arg-type]


def counts(result) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Stage sizes, from either result shape.

    `activation` returns an `ActivationSummary` wrapping a funnel;
    `acquisition` returns the funnel itself. One helper rather than two,
    because every assertion below is about the same five numbers.
    """
    funnel = getattr(result, "funnel", result)
    return {stage.stage: stage.subjects for stage in funnel.stages}


class TestTheActivationFunnel:
    async def test_a_complete_journey_reaches_every_stage(
        self, contract_session: AsyncSession
    ) -> None:
        await store(contract_session, Journey().verified().queued().played())

        assert counts(await activation(contract_session)) == {
            "user_registered": 1,
            "email_verified": 1,
            "queue_joined": 1,
            "match_started": 1,
            "activated": 1,
        }

    async def test_a_registration_alone_reaches_one_stage(
        self, contract_session: AsyncSession
    ) -> None:
        await store(contract_session, Journey())

        assert counts(await activation(contract_session)) == {
            "user_registered": 1,
            "email_verified": 0,
            "queue_joined": 0,
            "match_started": 0,
            "activated": 0,
        }

    async def test_activation_does_not_require_verification(
        self, contract_session: AsyncSession
    ) -> None:
        """The stages are ordered but not each other's precondition in the
        domain — an unverified account cannot play *rated*, and this funnel
        does not assert otherwise. What the query requires is that each
        event belong to the registration, not that the previous stage
        happened."""
        await store(contract_session, Journey().queued().played())

        result = counts(await activation(contract_session))
        assert result["email_verified"] == 0
        assert result["activated"] == 1


class TestUniqueSubjects:
    """**MUTATION A targets these.**"""

    async def test_twenty_queue_joins_are_one_person(self, contract_session: AsyncSession) -> None:
        """§13. A funnel counts people, and a `COUNT(*)` over events would
        report twenty at the queue stage and look entirely plausible."""
        await store(contract_session, Journey().verified().queued(times=20))

        assert counts(await activation(contract_session))["queue_joined"] == 1

    async def test_three_completed_matches_activate_once(
        self, contract_session: AsyncSession
    ) -> None:
        journey = Journey().verified().queued()
        for hours in (3, 5, 7):
            journey.played(after=timedelta(hours=hours))
        await store(contract_session, journey)

        assert counts(await activation(contract_session))["activated"] == 1

    async def test_two_people_are_two(self, contract_session: AsyncSession) -> None:
        await store(
            contract_session,
            Journey().verified().queued().played(),
            Journey().verified().queued().played(),
        )

        assert counts(await activation(contract_session))["activated"] == 2


class TestWhatDoesNotActivate:
    """**MUTATION B and E target these.**"""

    async def test_an_aborted_match_does_not_activate(self, contract_session: AsyncSession) -> None:
        """A64-027.1 §32: `abort` is `MatchOutcome.NONE` — no result, no
        rating change, a match that did not happen."""
        await store(contract_session, Journey().verified().queued().played(termination="abort"))

        result = counts(await activation(contract_session))
        assert result["match_started"] == 1
        assert result["activated"] == 0

    async def test_a_started_match_that_never_ended_does_not_activate(
        self, contract_session: AsyncSession
    ) -> None:
        """**MUTATION E.** Starting is not activating: A64-027.1 §31 chose
        the first *completed* match because a game abandoned at ply two has
        shown nobody what Arena64 is."""
        journey = Journey().verified().queued()
        journey._add(
            EventName.MATCH_STARTED,
            COHORT_DAY + timedelta(hours=3),
            {"match_id": str(uuid4()), "variant": "russian_8x8", "rated": True},
        )
        await store(contract_session, journey)

        result = counts(await activation(contract_session))
        assert result["match_started"] == 1
        assert result["activated"] == 0

    async def test_an_abort_then_a_resignation_activates_on_the_second(
        self, contract_session: AsyncSession
    ) -> None:
        journey = Journey().verified().queued()
        journey.played(termination="abort", after=timedelta(hours=3))
        journey.played(termination="resignation", after=timedelta(hours=6))
        await store(contract_session, journey)

        assert counts(await activation(contract_session))["activated"] == 1

    @pytest.mark.parametrize(
        "termination",
        [
            "resignation",
            "no_legal_moves",
            "all_pieces_captured",
            "agreed_draw",
            "repetition",
            "move_limit",
            "flag",
            "flag_insufficient_material",
            "abandonment",
            "adjudication",
        ],
    )
    async def test_every_qualifying_termination_activates(
        self, contract_session: AsyncSession, termination: str
    ) -> None:
        """§9's matrix, one row at a time. A resignation is the one
        somebody reads as a failure and it is a result."""
        await store(contract_session, Journey().verified().queued().played(termination=termination))

        assert counts(await activation(contract_session))["activated"] == 1


class TestTheFanOut:
    """§36 — one match can activate nought, one or two players."""

    async def test_a_match_activates_both_new_players(self, contract_session: AsyncSession) -> None:
        opponent = Journey().verified().queued()
        player = Journey().verified().queued()
        player.played(opponent=opponent)
        await store(contract_session, player, opponent)

        assert counts(await activation(contract_session))["activated"] == 2

    async def test_a_match_activates_only_the_player_who_was_new(
        self, contract_session: AsyncSession
    ) -> None:
        """The other seat activated on an earlier match, so this one adds
        nobody — the count is of people, not of matches."""
        veteran = Journey().verified().queued().played(after=timedelta(hours=3))
        newcomer = Journey().verified().queued()
        newcomer.played(after=timedelta(hours=8), opponent=veteran)
        await store(contract_session, veteran, newcomer)

        assert counts(await activation(contract_session))["activated"] == 2

    async def test_a_completion_with_no_start_activates_nobody(
        self, contract_session: AsyncSession
    ) -> None:
        """A completion carries no player. Without the start row there is
        nothing to attribute it to, and the query says nought rather than
        guessing a seat."""
        journey = Journey().verified().queued()
        journey._add(
            EventName.MATCH_COMPLETED,
            COHORT_DAY + timedelta(hours=4),
            {
                "match_id": str(uuid4()),
                "variant": "russian_8x8",
                "rated": True,
                "outcome": "win",
                "termination_reason": "resignation",
                "ply_count": 10,
                "origin": "queue",
            },
            entity=True,
        )
        await store(contract_session, journey)

        assert counts(await activation(contract_session))["activated"] == 0


class TestStrictOrdering:
    """**MUTATION F targets these.**"""

    async def test_an_event_before_registration_does_not_count(
        self, contract_session: AsyncSession
    ) -> None:
        """§12. A verification that precedes its own registration is
        physically impossible; the funnel excludes it rather than letting
        it inflate a stage, and raw events are never edited."""
        journey = Journey()
        journey._add(
            EventName.EMAIL_VERIFIED,
            COHORT_DAY - timedelta(hours=1),
            {"hours_since_registration": 0},
        )
        await store(contract_session, journey)

        assert counts(await activation(contract_session))["email_verified"] == 0

    async def test_the_exclusion_is_reported_as_a_data_quality_signal(
        self, contract_session: AsyncSession
    ) -> None:
        journey = Journey()
        journey._add(
            EventName.EMAIL_VERIFIED,
            COHORT_DAY - timedelta(hours=1),
            {"hours_since_registration": 0},
        )
        await store(contract_session, journey)

        quality = await service(contract_session).data_quality(
            environment=PRODUCTION, since=COHORT_DAY.date(), until=COHORT_DAY.date()
        )
        assert quality.out_of_order_subjects == 1
        assert not quality.is_clean

    async def test_a_stage_outside_the_window_does_not_count(
        self, contract_session: AsyncSession
    ) -> None:
        """§17. The window closes a cohort, or a denominator stays open
        forever and no rate is ever final."""
        await store(
            contract_session,
            Journey().verified().queued().played(after=timedelta(days=400)),
        )

        result = counts(await activation(contract_session))
        assert result["queue_joined"] == 1
        assert result["match_started"] == 0
        assert result["activated"] == 0

    async def test_somebody_elses_events_do_not_count(self, contract_session: AsyncSession) -> None:
        """The subject constraint. Without it every stage would count the
        whole platform."""
        registered_only = Journey()
        other = Journey().verified().queued().played()
        await store(contract_session, registered_only, other)

        result = counts(await activation(contract_session))
        assert result["user_registered"] == 2
        assert result["email_verified"] == 1
        assert result["activated"] == 1


class TestTheFilters:
    """**MUTATION C and D target these.**"""

    async def test_synthetic_traffic_is_excluded_by_default(
        self, contract_session: AsyncSession
    ) -> None:
        await store(
            contract_session,
            Journey().verified().queued().played(),
            Journey(synthetic=True).verified().queued().played(),
        )

        assert counts(await activation(contract_session))["activated"] == 1

    async def test_synthetic_traffic_can_be_asked_for_explicitly(
        self, contract_session: AsyncSession
    ) -> None:
        await store(
            contract_session,
            Journey().verified().queued().played(),
            Journey(synthetic=True).verified().queued().played(),
        )

        result = await activation(contract_session, include_synthetic=True)
        assert counts(result)["activated"] == 2
        assert result.funnel.meta.include_synthetic is True

    async def test_another_environment_is_excluded(self, contract_session: AsyncSession) -> None:
        """§25. A laptop's events in a production number is the failure
        this filter exists for, and it is invisible without a test."""
        await store(
            contract_session,
            Journey().verified().queued().played(),
            Journey(environment=Environment.STAGING).verified().queued().played(),
        )

        assert counts(await activation(contract_session))["activated"] == 1

    async def test_every_stage_filters_and_not_only_the_cohort(
        self, contract_session: AsyncSession
    ) -> None:
        """A production registration whose later events were recorded in
        staging must not reach those stages.

        A subject is not scoped to an environment — `analytics.subject`
        has no such column — so this is representable, and a filter only on
        the cohort would let it through. Found by a mutation check: the
        test above passed with every per-stage filter removed, because the
        cohort filter alone dropped the whole journey.
        """
        journey = Journey()
        for name, after, properties in (
            (EventName.EMAIL_VERIFIED, timedelta(hours=1), {"hours_since_registration": 1}),
            (
                EventName.QUEUE_JOINED,
                timedelta(hours=2),
                {"variant": "russian_8x8", "queue_type": "ranked", "rated": True},
            ),
        ):
            journey.events.append(
                AnalyticsEvent(
                    event_id=uuid4(),
                    event_name=name,
                    event_version=1,
                    occurred_at=journey.registered_at + after,
                    received_at=journey.registered_at + after,
                    source="backend",
                    # **Staging**, while the registration above is production.
                    environment=Environment.STAGING,
                    subject_key=journey.subject,
                    properties=properties,
                )
            )
        await store(contract_session, journey)

        result = counts(await activation(contract_session))
        assert result["user_registered"] == 1
        assert result["email_verified"] == 0
        assert result["queue_joined"] == 0


class TestCohortBoundaries:
    """§56 — UTC, and half-open."""

    async def test_a_registration_at_the_last_second_is_in_that_day(
        self, contract_session: AsyncSession
    ) -> None:
        late = datetime(2026, 3, 1, 23, 59, 59, tzinfo=UTC)
        await store(contract_session, Journey(registered_at=late))

        assert counts(await activation(contract_session))["user_registered"] == 1

    async def test_a_registration_at_midnight_belongs_to_the_next_day(
        self, contract_session: AsyncSession
    ) -> None:
        midnight = datetime(2026, 3, 2, 0, 0, 0, tzinfo=UTC)
        await store(contract_session, Journey(registered_at=midnight))

        assert counts(await activation(contract_session))["user_registered"] == 0
        later = await activation(contract_session, since=midnight.date(), until=midnight.date())
        assert counts(later)["user_registered"] == 1


class TestDurations:
    async def test_time_to_activation_is_measured_between_server_instants(
        self, contract_session: AsyncSession
    ) -> None:
        # Registered, then a match that starts at +3h and ends 20 minutes
        # later: activation is the completion, so 3h20m.
        await store(contract_session, Journey().verified().queued().played())

        result = await activation(contract_session)
        assert result.time_to_activation.sample == 1
        assert result.time_to_activation.median_seconds == pytest.approx((3 * 3600) + 1200)

    async def test_time_to_verify(self, contract_session: AsyncSession) -> None:
        await store(contract_session, Journey().verified(after=timedelta(minutes=30)))

        result = await activation(contract_session)
        assert result.time_to_verify.median_seconds == pytest.approx(1800)

    async def test_an_empty_sample_has_no_percentile(self, contract_session: AsyncSession) -> None:
        await store(contract_session, Journey())

        result = await activation(contract_session)
        assert result.time_to_activation.sample == 0
        assert result.time_to_activation.median_seconds is None

    async def test_the_median_is_a_percentile_not_a_mean(
        self, contract_session: AsyncSession
    ) -> None:
        """Three activations at 1h, 2h and 10h: a mean is 4h20m and the
        median is 2h. §55 — a mean over a skewed distribution describes
        nobody."""
        for hours in (1, 2, 10):
            journey = Journey().verified().queued()
            journey.played(after=timedelta(hours=hours) - timedelta(minutes=20))
            await store(contract_session, journey)

        result = await activation(contract_session)
        assert result.time_to_activation.sample == 3
        assert result.time_to_activation.median_seconds == pytest.approx(2 * 3600)


class TestErasure:
    """§27 — privacy over completeness."""

    async def test_an_erased_subject_still_counts_in_aggregate(
        self, contract_session: AsyncSession
    ) -> None:
        """Erasure deletes the `analytics.subject` row and leaves the
        events. The funnel groups by `subject_key`, which still groups
        them as one person — it simply no longer names which person. That
        is D3's aggregate-preserving half, and the reason a cohort's rate
        does not shift when a member leaves.
        """
        from app.modules.analytics.infrastructure.repositories.analytics_repository import (
            SqlAlchemySubjectDirectory,
            SqlAlchemySubjectEraser,
        )

        player = uuid4()
        directory = SqlAlchemySubjectDirectory(contract_session)
        key = await directory.resolve(player)

        journey = Journey().verified().queued().played()
        object.__setattr__(journey, "subject", SubjectKey(key))
        rebuilt = [
            AnalyticsEvent(
                event_id=event.event_id,
                event_name=event.event_name,
                event_version=event.event_version,
                occurred_at=event.occurred_at,
                received_at=event.received_at,
                source=event.source,
                environment=event.environment,
                subject_key=None if event.subject_key is None else SubjectKey(key),
                properties=event.properties,
            )
            for event in journey.all_events()
        ]
        await SqlAlchemyAnalyticsEventStore(contract_session).append(rebuilt)

        before = counts(await activation(contract_session))
        await SqlAlchemySubjectEraser(contract_session).erase(player)
        after = counts(await activation(contract_session))

        assert before == after
        assert after["activated"] == 1
        # And the link is gone: nothing can name the person any more.
        assert await directory.lookup(player) is None


class TestTheAcquisitionFunnel:
    """§52's edge cases."""

    def _anonymous(
        self, *, at: datetime = COHORT_DAY, browser: UUID | None = None
    ) -> tuple[UUID, list[AnalyticsEvent]]:
        anonymous_id = browser or uuid4()
        return anonymous_id, [
            AnalyticsEvent(
                event_id=uuid4(),
                event_name=EventName.LANDING_VIEWED,
                event_version=1,
                occurred_at=at,
                received_at=at,
                source="frontend",
                environment=Environment.PRODUCTION,
                anonymous_id=anonymous_id,
                properties={},
            )
        ]

    def _clicked(self, anonymous_id: UUID, *, at: datetime) -> AnalyticsEvent:
        return AnalyticsEvent(
            event_id=uuid4(),
            event_name=EventName.REGISTER_CTA_CLICKED,
            event_version=1,
            occurred_at=at,
            received_at=at,
            source="frontend",
            environment=Environment.PRODUCTION,
            anonymous_id=anonymous_id,
            properties={"placement": "hero"},
        )

    async def _acquisition(self, session: AsyncSession):  # type: ignore[no-untyped-def]
        return await service(session).acquisition(
            environment=PRODUCTION, since=COHORT_DAY.date(), until=COHORT_DAY.date()
        )

    async def test_a_landing_alone_reaches_one_stage(self, contract_session: AsyncSession) -> None:
        _, events = self._anonymous()
        await SqlAlchemyAnalyticsEventStore(contract_session).append(events)

        assert counts(await self._acquisition(contract_session)) == {
            "landing_viewed": 1,
            "register_cta_clicked": 0,
            "user_registered": 0,
        }

    async def test_a_repeated_click_is_one_browser(self, contract_session: AsyncSession) -> None:
        browser, events = self._anonymous()
        events += [
            self._clicked(browser, at=COHORT_DAY + timedelta(minutes=minute))
            for minute in (1, 2, 3)
        ]
        await SqlAlchemyAnalyticsEventStore(contract_session).append(events)

        assert counts(await self._acquisition(contract_session))["register_cta_clicked"] == 1

    async def test_a_registration_without_an_observed_landing_is_not_in_the_funnel(
        self, contract_session: AsyncSession
    ) -> None:
        """§52. It still counts in the activation funnel's first stage —
        it is a real registration — but forcing it into an acquisition
        denominator it never entered would make the conversion a fiction.
        """
        await store(contract_session, Journey())

        acquisition = counts(await self._acquisition(contract_session))
        assert acquisition == {
            "landing_viewed": 0,
            "register_cta_clicked": 0,
            "user_registered": 0,
        }
        assert counts(await activation(contract_session))["user_registered"] == 1

    async def test_a_rotated_identity_is_a_second_browser(
        self, contract_session: AsyncSession
    ) -> None:
        """§52, and the honest cost of `anonymous_id` rotation: the same
        person after a sign-out is a new browser, and M1 overcounts by one.
        A64-027.1 §36 states it rather than hiding it."""
        _, first = self._anonymous()
        _, second = self._anonymous()
        await SqlAlchemyAnalyticsEventStore(contract_session).append(first + second)

        assert counts(await self._acquisition(contract_session))["landing_viewed"] == 2

    async def test_the_stitch_attributes_a_registration_to_its_browser(
        self, contract_session: AsyncSession
    ) -> None:
        """§14, and the mechanism A64-027.1 §9 designed.

        `user_registered` is a backend projection with a subject and no
        browser. The link is derived from a row that carries **both** — a
        client event fired while signed in — and resolved at query time,
        so raw history is never rewritten.
        """
        journey = Journey()
        browser, events = self._anonymous(at=COHORT_DAY - timedelta(minutes=10))
        events.append(self._clicked(browser, at=COHORT_DAY - timedelta(minutes=2)))
        # The row that carries both: a signed-in player sharing something.
        events.append(
            AnalyticsEvent(
                event_id=uuid4(),
                event_name=EventName.SHARE_CLICKED,
                event_version=1,
                occurred_at=COHORT_DAY + timedelta(hours=1),
                received_at=COHORT_DAY + timedelta(hours=1),
                source="frontend",
                environment=Environment.PRODUCTION,
                subject_key=journey.subject,
                anonymous_id=browser,
                properties={"surface": "tournament", "mechanism": "clipboard"},
            )
        )
        await store(contract_session, journey)
        await SqlAlchemyAnalyticsEventStore(contract_session).append(events)

        result = await service(contract_session).acquisition(
            environment=PRODUCTION,
            since=(COHORT_DAY - timedelta(days=1)).date(),
            until=COHORT_DAY.date(),
        )

        assert counts(result)["user_registered"] == 1

    async def test_an_unstitched_registration_is_reported_separately(
        self, contract_session: AsyncSession
    ) -> None:
        """The coverage gap, made visible rather than absorbed.

        Without a row carrying both identities the funnel cannot attribute
        the registration to the browser — and the meta says how many
        registrations there were, so nobody reads the third stage as all of
        them.
        """
        browser, events = self._anonymous(at=COHORT_DAY - timedelta(minutes=10))
        events.append(self._clicked(browser, at=COHORT_DAY - timedelta(minutes=2)))
        await SqlAlchemyAnalyticsEventStore(contract_session).append(events)
        await store(contract_session, Journey())

        result = await service(contract_session).acquisition(
            environment=PRODUCTION,
            since=(COHORT_DAY - timedelta(days=1)).date(),
            until=COHORT_DAY.date(),
        )

        assert counts(result)["register_cta_clicked"] == 1
        assert counts(result)["user_registered"] == 0
        assert result.meta.registrations_in_range == 1

    async def test_synthetic_acquisition_is_excluded(self, contract_session: AsyncSession) -> None:
        browser, events = self._anonymous()
        synthetic = AnalyticsEvent(
            event_id=uuid4(),
            event_name=EventName.LANDING_VIEWED,
            event_version=1,
            occurred_at=COHORT_DAY,
            received_at=COHORT_DAY,
            source="frontend",
            environment=Environment.PRODUCTION,
            anonymous_id=uuid4(),
            is_synthetic=True,
            properties={},
        )
        await SqlAlchemyAnalyticsEventStore(contract_session).append([*events, synthetic])

        assert counts(await self._acquisition(contract_session))["landing_viewed"] == 1


class TestProvenance:
    async def test_the_result_says_which_environment_it_measured(
        self, contract_session: AsyncSession
    ) -> None:
        await store(contract_session, Journey())
        result = await activation(contract_session)

        assert result.funnel.meta.environment == PRODUCTION
        assert result.funnel.meta.include_synthetic is False
        assert result.funnel.meta.coverage is Coverage.COMPLETE
        assert result.funnel.meta.generated_at == NOW
