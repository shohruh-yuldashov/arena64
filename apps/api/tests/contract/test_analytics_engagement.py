"""Engagement and retention over real PostgreSQL — A64-027.4.

The arithmetic is unit-tested. This proves the SQL, and the assertions that
matter are the ones an in-memory implementation would pass while the query
was wrong: distinct-subject counting, the calendar-day retention offsets,
`NULL` for an unelapsed window, and the activity predicate being the same
three events everywhere.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.environment import Environment
from app.modules.analytics.application.services.engagement import EngagementService
from app.modules.analytics.domain.event import AnalyticsEvent
from app.modules.analytics.domain.subject import SubjectKey
from app.modules.analytics.infrastructure.repositories.analytics_repository import (
    SqlAlchemyAnalyticsEventStore,
)
from app.modules.analytics.infrastructure.repositories.engagement_repository import (
    SqlAlchemyEngagementReader,
)
from app.platform.analytics import EventName

#: A fixed "today", so maturity and the retention offsets are deterministic.
TODAY = date(2026, 9, 5)
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
#: Far enough back that D30 has elapsed against `TODAY`.
COHORT = date(2026, 7, 1)
PRODUCTION = Environment.PRODUCTION.value


class _FrozenClock:
    def now(self) -> datetime:
        return NOW


def event(
    name: EventName,
    day: date,
    *,
    subject: SubjectKey | None = None,
    hour: int = 10,
    properties: dict[str, object] | None = None,
    environment: Environment = Environment.PRODUCTION,
    synthetic: bool = False,
) -> AnalyticsEvent:
    at = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=hour)
    return AnalyticsEvent(
        event_id=uuid4(),
        event_name=name,
        event_version=1,
        occurred_at=at,
        received_at=at,
        source="backend",
        environment=environment,
        subject_key=subject,
        is_synthetic=synthetic,
        properties=properties or {},
    )


async def store(session: AsyncSession, *events: AnalyticsEvent) -> None:
    await SqlAlchemyAnalyticsEventStore(session).append(list(events))


def service(session: AsyncSession) -> EngagementService:
    return EngagementService(reader=SqlAlchemyEngagementReader(session), clock=_FrozenClock())


def subject() -> SubjectKey:
    return SubjectKey(uuid4())


def match(day: date, subject_key: SubjectKey, *, hour: int = 10) -> AnalyticsEvent:
    return event(
        EventName.MATCH_STARTED,
        day,
        subject=subject_key,
        hour=hour,
        properties={"match_id": str(uuid4()), "variant": "russian_8x8", "rated": True},
    )


class TestActivePlayers:
    async def test_the_three_windows_end_on_the_same_day(
        self, contract_session: AsyncSession
    ) -> None:
        person = subject()
        await store(
            contract_session,
            match(TODAY, person),
            match(TODAY - timedelta(days=3), subject()),
            match(TODAY - timedelta(days=20), subject()),
            match(TODAY - timedelta(days=40), subject()),
        )

        result = await service(contract_session).active_players(environment=PRODUCTION, as_of=TODAY)

        assert (result.daily, result.weekly, result.monthly) == (1, 2, 3)

    async def test_all_three_activity_signals_count(self, contract_session: AsyncSession) -> None:
        """A64-027.1 §30's three, and the challenge one only became
        measurable in A64-027.4 — before it, DAU undercounted anybody whose
        day was a challenge."""
        await store(
            contract_session,
            match(TODAY, subject()),
            event(
                EventName.TOURNAMENT_ENTERED,
                TODAY,
                subject=subject(),
                properties={"tournament_id": str(uuid4())},
            ),
            event(
                EventName.CHALLENGE_SENT,
                TODAY,
                subject=subject(),
                properties={"variant": "russian_8x8", "rated": True},
            ),
        )

        result = await service(contract_session).active_players(environment=PRODUCTION, as_of=TODAY)
        assert result.daily == 3

    async def test_opening_a_page_is_not_activity(self, contract_session: AsyncSession) -> None:
        """If it were, DAU would measure the landing page."""
        landed = AnalyticsEvent(
            event_id=uuid4(),
            event_name=EventName.LANDING_VIEWED,
            event_version=1,
            occurred_at=NOW,
            received_at=NOW,
            source="frontend",
            environment=Environment.PRODUCTION,
            anonymous_id=uuid4(),
            properties={},
        )
        await store(contract_session, landed)

        result = await service(contract_session).active_players(environment=PRODUCTION, as_of=TODAY)
        assert result.daily == 0

    async def test_completing_a_match_alone_is_not_activity(
        self, contract_session: AsyncSession
    ) -> None:
        """`match_completed` is entity-level and names nobody, so it cannot
        make anybody active — which is also why §30 chose the start."""
        await store(
            contract_session,
            event(
                EventName.MATCH_COMPLETED,
                TODAY,
                properties={
                    "match_id": str(uuid4()),
                    "variant": "russian_8x8",
                    "rated": True,
                    "outcome": "win",
                    "termination_reason": "resignation",
                    "ply_count": 20,
                    "origin": "queue",
                },
            ),
        )

        result = await service(contract_session).active_players(environment=PRODUCTION, as_of=TODAY)
        assert result.daily == 0

    async def test_twenty_matches_are_one_active_player(
        self, contract_session: AsyncSession
    ) -> None:
        person = subject()
        await store(contract_session, *(match(TODAY, person, hour=h) for h in range(1, 21)))

        result = await service(contract_session).active_players(environment=PRODUCTION, as_of=TODAY)
        assert (result.daily, result.monthly) == (1, 1)

    async def test_synthetic_and_other_environments_are_excluded(
        self, contract_session: AsyncSession
    ) -> None:
        await store(
            contract_session,
            match(TODAY, subject()),
            event(
                EventName.MATCH_STARTED,
                TODAY,
                subject=subject(),
                synthetic=True,
                properties={"match_id": str(uuid4()), "variant": "russian_8x8", "rated": True},
            ),
            event(
                EventName.MATCH_STARTED,
                TODAY,
                subject=subject(),
                environment=Environment.STAGING,
                properties={"match_id": str(uuid4()), "variant": "russian_8x8", "rated": True},
            ),
        )

        result = await service(contract_session).active_players(environment=PRODUCTION, as_of=TODAY)
        assert result.daily == 1


class TestRetention:
    async def _table(self, session: AsyncSession, **overrides: object):  # type: ignore[no-untyped-def]
        arguments: dict[str, object] = {
            "environment": PRODUCTION,
            "since": COHORT,
            "until": COHORT,
        }
        arguments.update(overrides)
        return await service(session).retention(**arguments)  # type: ignore[arg-type]

    async def test_a_returner_on_the_exact_day_counts(self, contract_session: AsyncSession) -> None:
        """A64-027.1 §33: D7 is **that** calendar day, not "within seven
        days" — the distinction a reader assumes wrongly."""
        person = subject()
        await store(
            contract_session,
            event(EventName.USER_REGISTERED, COHORT, subject=person),
            match(COHORT + timedelta(days=7), person),
        )

        row = (await self._table(contract_session)).rows[0]
        assert (row.cohort, row.d1, row.d7, row.d30) == (1, 0, 1, 0)

    async def test_a_returner_on_a_nearby_day_does_not_count_for_d7(
        self, contract_session: AsyncSession
    ) -> None:
        person = subject()
        await store(
            contract_session,
            event(EventName.USER_REGISTERED, COHORT, subject=person),
            match(COHORT + timedelta(days=6), person),
        )

        row = (await self._table(contract_session)).rows[0]
        assert row.d7 == 0

    async def test_the_cohort_is_the_registration_day_in_utc(
        self, contract_session: AsyncSession
    ) -> None:
        """§33 and §56: a registration at 23:59 UTC is that day's, and one
        at 00:00 the next day belongs to the next cohort."""
        late, early = subject(), subject()
        await store(
            contract_session,
            event(EventName.USER_REGISTERED, COHORT, subject=late, hour=23),
            event(EventName.USER_REGISTERED, COHORT + timedelta(days=1), subject=early, hour=0),
        )

        table = await self._table(contract_session, until=COHORT + timedelta(days=1))
        assert [row.cohort_day for row in table.rows] == [COHORT, COHORT + timedelta(days=1)]
        assert [row.cohort for row in table.rows] == [1, 1]

    async def test_an_unelapsed_window_is_null_rather_than_zero(
        self, contract_session: AsyncSession
    ) -> None:
        """**The distinction this whole feature turns on.** A cohort from
        yesterday has not failed its D30; it has not had one, and a zero
        there is a decline that did not happen."""
        recent = TODAY - timedelta(days=2)
        await store(contract_session, event(EventName.USER_REGISTERED, recent, subject=subject()))

        row = (await self._table(contract_session, since=recent, until=recent)).rows[0]
        assert row.d1 == 0
        assert row.d7 is None
        assert row.d30 is None
        assert row.rate(7) is None

    async def test_nobody_returning_is_a_measured_zero(
        self, contract_session: AsyncSession
    ) -> None:
        await store(contract_session, event(EventName.USER_REGISTERED, COHORT, subject=subject()))

        row = (await self._table(contract_session)).rows[0]
        assert (row.d1, row.d7, row.d30) == (0, 0, 0)
        assert row.rate(1) == 0.0

    async def test_returning_twice_on_one_day_is_one_returner(
        self, contract_session: AsyncSession
    ) -> None:
        person = subject()
        await store(
            contract_session,
            event(EventName.USER_REGISTERED, COHORT, subject=person),
            match(COHORT + timedelta(days=1), person, hour=9),
            match(COHORT + timedelta(days=1), person, hour=18),
        )

        row = (await self._table(contract_session)).rows[0]
        assert row.d1 == 1

    async def test_somebody_elses_activity_does_not_retain_a_cohort(
        self, contract_session: AsyncSession
    ) -> None:
        await store(
            contract_session,
            event(EventName.USER_REGISTERED, COHORT, subject=subject()),
            match(COHORT + timedelta(days=1), subject()),
        )

        row = (await self._table(contract_session)).rows[0]
        assert (row.cohort, row.d1) == (1, 0)

    async def test_a_never_activating_registration_stays_in_the_denominator(
        self, contract_session: AsyncSession
    ) -> None:
        """§33's reason for a registration cohort rather than an activation
        one: the player who never activates is retention's most important
        data point, and an activation cohort excludes them silently."""
        returner, ghost = subject(), subject()
        await store(
            contract_session,
            event(EventName.USER_REGISTERED, COHORT, subject=returner),
            event(EventName.USER_REGISTERED, COHORT, subject=ghost),
            match(COHORT + timedelta(days=1), returner),
        )

        row = (await self._table(contract_session)).rows[0]
        assert (row.cohort, row.d1) == (2, 1)
        assert row.rate(1) == pytest.approx(0.5)

    async def test_synthetic_registrations_are_excluded(
        self, contract_session: AsyncSession
    ) -> None:
        await store(
            contract_session,
            event(EventName.USER_REGISTERED, COHORT, subject=subject()),
            event(EventName.USER_REGISTERED, COHORT, subject=subject(), synthetic=True),
        )

        row = (await self._table(contract_session)).rows[0]
        assert row.cohort == 1

    async def test_a_synthetic_return_does_not_retain_a_real_cohort(
        self, contract_session: AsyncSession
    ) -> None:
        """The per-join filter, not only the cohort one — the gap
        A64-027.3's mutation check found in the funnels."""
        person = subject()
        await store(
            contract_session,
            event(EventName.USER_REGISTERED, COHORT, subject=person),
            event(
                EventName.MATCH_STARTED,
                COHORT + timedelta(days=1),
                subject=person,
                synthetic=True,
                properties={"match_id": str(uuid4()), "variant": "russian_8x8", "rated": True},
            ),
        )

        row = (await self._table(contract_session)).rows[0]
        assert row.d1 == 0


class TestWeeklyEngagement:
    WEEK = COHORT

    async def _summary(self, session: AsyncSession, **overrides: object):  # type: ignore[no-untyped-def]
        arguments: dict[str, object] = {"environment": PRODUCTION, "week_start": self.WEEK}
        arguments.update(overrides)
        return await service(session).engagement(**arguments)  # type: ignore[arg-type]

    async def test_matches_per_active_player(self, contract_session: AsyncSession) -> None:
        busy, quiet = subject(), subject()
        await store(
            contract_session,
            *(match(self.WEEK, busy, hour=h) for h in (9, 10, 11)),
            match(self.WEEK + timedelta(days=1), quiet),
        )

        summary = await self._summary(contract_session)
        assert summary.active_players == 2
        assert summary.match_starts == 4
        assert summary.matches_per_active_player == pytest.approx(2.0)
        # The median over per-player counts is 2 — three and one.
        assert summary.median_matches_per_active_player == pytest.approx(2.0)

    async def test_the_median_is_not_the_mean(self, contract_session: AsyncSession) -> None:
        """§29's limitation on M22, demonstrated: one heavy player drags
        the mean and leaves the median where most people are."""
        heavy = subject()
        light = [subject() for _ in range(3)]
        await store(
            contract_session,
            *(match(self.WEEK, heavy, hour=h) for h in range(1, 13)),
            *(match(self.WEEK, person) for person in light),
        )

        summary = await self._summary(contract_session)
        assert summary.matches_per_active_player == pytest.approx(15 / 4)
        assert summary.median_matches_per_active_player == pytest.approx(1.0)

    async def test_tournament_participation(self, contract_session: AsyncSession) -> None:
        entrant, other = subject(), subject()
        await store(
            contract_session,
            event(
                EventName.TOURNAMENT_ENTERED,
                self.WEEK,
                subject=entrant,
                properties={"tournament_id": str(uuid4())},
            ),
            match(self.WEEK, other),
        )

        summary = await self._summary(contract_session)
        assert summary.active_players == 2
        assert summary.tournament_entrants == 1
        assert summary.tournament_participation == pytest.approx(0.5)

    async def test_challenge_acceptance_keeps_the_refusals_apart(
        self, contract_session: AsyncSession
    ) -> None:
        person = subject()
        sent = [
            event(
                EventName.CHALLENGE_SENT,
                self.WEEK,
                subject=person,
                hour=h,
                properties={"variant": "russian_8x8", "rated": True},
            )
            for h in range(1, 5)
        ]
        resolutions = [
            event(
                EventName.CHALLENGE_RESOLVED,
                self.WEEK,
                subject=person,
                hour=h,
                properties={"resolution": resolution},
            )
            for h, resolution in enumerate(("accepted", "accepted", "declined", "expired"), 10)
        ]
        await store(contract_session, *sent, *resolutions)

        summary = await self._summary(contract_session)
        assert summary.challenges_sent == 4
        assert summary.challenge_acceptance == pytest.approx(0.5)
        assert (summary.challenges_declined, summary.challenges_expired) == (1, 1)

    async def test_friendships_are_counted_not_deduplicated(
        self, contract_session: AsyncSession
    ) -> None:
        """M16 is `COUNT(friendship_created)`: the graph grew by two edges
        even if one person made both."""
        person = subject()
        await store(
            contract_session,
            event(EventName.FRIENDSHIP_CREATED, self.WEEK, subject=person, hour=9),
            event(EventName.FRIENDSHIP_CREATED, self.WEEK, subject=person, hour=10),
        )

        summary = await self._summary(contract_session)
        assert summary.friendships_created == 2

    async def test_the_week_is_seven_calendar_days(self, contract_session: AsyncSession) -> None:
        await store(
            contract_session,
            match(self.WEEK, subject()),
            match(self.WEEK + timedelta(days=6), subject()),
            match(self.WEEK + timedelta(days=7), subject()),
            match(self.WEEK - timedelta(days=1), subject()),
        )

        summary = await self._summary(contract_session)
        assert summary.week_end == self.WEEK + timedelta(days=6)
        assert summary.active_players == 2

    async def test_an_empty_week_reports_no_rates(self, contract_session: AsyncSession) -> None:
        summary = await self._summary(contract_session)

        assert summary.active_players == 0
        assert summary.matches_per_active_player is None
        assert summary.tournament_participation is None
        assert summary.challenge_acceptance is None
        assert summary.median_matches_per_active_player is None
