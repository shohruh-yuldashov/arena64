"""Matchmaking and game health over real PostgreSQL — A64-027.5.

The arithmetic is unit-tested. This proves the SQL, and the assertions that
matter are the ones an in-memory implementation would pass while the query
was wrong: a match counted once rather than twice despite two seat rows, a
matched ticket never appearing as abandonment, and a segmented rate whose
denominator is segmented too.

Skipped, not failed, when PostgreSQL is unreachable.
"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.environment import Environment
from app.modules.analytics.application.services.matchmaking import MatchmakingService
from app.modules.analytics.domain.event import AnalyticsEvent
from app.modules.analytics.domain.properties import SpeedClass
from app.modules.analytics.domain.subject import SubjectKey
from app.modules.analytics.infrastructure.repositories.analytics_repository import (
    SqlAlchemyAnalyticsEventStore,
)
from app.modules.analytics.infrastructure.repositories.matchmaking_repository import (
    SqlAlchemyMatchmakingReader,
)
from app.platform.analytics import EventName

DAY = date(2026, 4, 1)
NOW = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
PRODUCTION = Environment.PRODUCTION.value


class _FrozenClock:
    def now(self) -> datetime:
        return NOW


def _at(hour: int = 10, day: date = DAY) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=hour)


def _event(
    name: EventName,
    properties: dict[str, object],
    *,
    subject: SubjectKey | None = None,
    hour: int = 10,
    day: date = DAY,
    environment: Environment = Environment.PRODUCTION,
    synthetic: bool = False,
) -> AnalyticsEvent:
    at = _at(hour, day)
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
        properties=properties,
    )


def subject() -> SubjectKey:
    return SubjectKey(uuid4())


# --- fixtures shaped like the real lifecycle ---------------------------------


def queue_join(person: SubjectKey, **kwargs: object) -> AnalyticsEvent:
    return _event(
        EventName.QUEUE_JOINED,
        {"variant": "russian_8x8", "queue_type": "ranked", "rated": True},
        subject=person,
        **kwargs,  # type: ignore[arg-type]
    )


def queue_left(person: SubjectKey, reason: str, waited_ms: int = 30_000, **kwargs: object):  # type: ignore[no-untyped-def]
    return _event(
        EventName.QUEUE_LEFT,
        {
            "reason": reason,
            "waited_ms": waited_ms,
            "variant": "russian_8x8",
            "queue_type": "ranked",
            "rated": True,
        },
        subject=person,
        **kwargs,  # type: ignore[arg-type]
    )


def pairing(light: SubjectKey, dark: SubjectKey, waited_ms: int, **kwargs: object):  # type: ignore[no-untyped-def]
    """Two `match_found` seats, both carrying the pair's own wait — the
    shape A64-027.2's fan-out produces."""
    match_id = str(uuid4())
    properties = {
        "match_id": match_id,
        "variant": "russian_8x8",
        "queue_type": "ranked",
        "waited_ms": waited_ms,
        "rated": True,
    }
    return match_id, [
        _event(EventName.MATCH_FOUND, dict(properties), subject=seat, **kwargs)  # type: ignore[arg-type]
        for seat in (light, dark)
    ]


def offer(resolution: str, match_id: str | None = None, **kwargs: object) -> AnalyticsEvent:
    return _event(
        EventName.MATCH_OFFER_RESOLVED,
        {"match_id": match_id or str(uuid4()), "resolution": resolution},
        **kwargs,  # type: ignore[arg-type]
    )


def started(light: SubjectKey, dark: SubjectKey, *, rated: bool = True, **kwargs: object):  # type: ignore[no-untyped-def]
    """Two seat rows for one match — the grain trap this suite exists for."""
    match_id = str(uuid4())
    properties = {"match_id": match_id, "variant": "russian_8x8", "rated": rated}
    return match_id, [
        _event(EventName.MATCH_STARTED, dict(properties), subject=seat, **kwargs)  # type: ignore[arg-type]
        for seat in (light, dark)
    ]


def completed(
    match_id: str,
    reason: str,
    *,
    outcome: str = "win",
    rated: bool = True,
    speed: str = "blitz",
    **kwargs: object,
) -> AnalyticsEvent:
    """One entity-level row — a completion carries no seat."""
    return _event(
        EventName.MATCH_COMPLETED,
        {
            "match_id": match_id,
            "variant": "russian_8x8",
            "rated": rated,
            "speed_class": speed,
            "outcome": outcome,
            "termination_reason": reason,
            "ply_count": 40,
            "origin": "queue",
        },
        **kwargs,  # type: ignore[arg-type]
    )


async def store(session: AsyncSession, *events: AnalyticsEvent) -> None:
    await SqlAlchemyAnalyticsEventStore(session).append(list(events))


def service(session: AsyncSession) -> MatchmakingService:
    return MatchmakingService(reader=SqlAlchemyMatchmakingReader(session), clock=_FrozenClock())


async def queue_health(session: AsyncSession, **overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {"environment": PRODUCTION, "since": DAY, "until": DAY}
    arguments.update(overrides)
    return await service(session).queue_health(**arguments)  # type: ignore[arg-type]


async def game_health(session: AsyncSession, **overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {"environment": PRODUCTION, "since": DAY, "until": DAY}
    arguments.update(overrides)
    return await service(session).game_health(**arguments)  # type: ignore[arg-type]


class TestQueueHealth:
    """**MUTATION A and B target these.**"""

    async def test_a_matched_ticket_is_never_abandonment(
        self, contract_session: AsyncSession
    ) -> None:
        """§13, and the failure it names: "match found, therefore the queue
        entry was removed, therefore abandoned". Structurally impossible —
        the pairing service publishes no ticket event — and asserted here
        so a future projection cannot reintroduce it."""
        light, dark = subject(), subject()
        _, seats = pairing(light, dark, 5_000)
        await store(contract_session, queue_join(light), queue_join(dark), *seats)

        health = await queue_health(contract_session)

        assert health.queue_joins == 2
        assert health.paired_attempts == 2
        assert health.abandoned_attempts == 0
        assert health.abandonment_rate == 0.0

    async def test_m7b_over_a_mixed_day(self, contract_session: AsyncSession) -> None:
        light, dark = subject(), subject()
        _, seats = pairing(light, dark, 4_000)
        quitter, waiter = subject(), subject()
        await store(
            contract_session,
            queue_join(light),
            queue_join(dark),
            *seats,
            queue_join(quitter),
            queue_left(quitter, "cancelled"),
            queue_join(waiter),
            queue_left(waiter, "expired"),
        )

        health = await queue_health(contract_session)

        assert health.queue_joins == 4
        assert (health.paired_attempts, health.abandoned_attempts) == (2, 2)
        assert health.abandonment_rate == pytest.approx(0.5)
        assert (health.cancelled_attempts, health.expired_attempts) == (1, 1)

    async def test_multiple_attempts_by_one_player_are_multiple_attempts(
        self, contract_session: AsyncSession
    ) -> None:
        """§15. Matchmaking measures attempts, unlike DAU: joining, leaving
        and joining again is two attempts, and collapsing them by subject
        would halve the denominator."""
        person, opponent = subject(), subject()
        _, seats = pairing(person, opponent, 2_000, hour=14)
        await store(
            contract_session,
            queue_join(person, hour=9),
            queue_left(person, "cancelled", hour=10),
            queue_join(person, hour=13),
            queue_join(opponent, hour=13),
            *seats,
        )

        health = await queue_health(contract_session)

        assert health.queue_joins == 3
        assert health.abandoned_attempts == 1
        assert health.paired_attempts == 2

    async def test_the_wait_sample_counts_pairings_not_seats(
        self, contract_session: AsyncSession
    ) -> None:
        """Both seats carry the pair's own wait, so counting rows would
        report twice the pairings. The percentiles are unaffected by that
        duplication; the sample size is not."""
        _, first = pairing(subject(), subject(), 3_000)
        _, second = pairing(subject(), subject(), 9_000)
        await store(contract_session, *first, *second)

        health = await queue_health(contract_session)

        assert health.paired_attempts == 4
        assert health.wait.sample == 2

    async def test_the_wait_percentiles_are_seconds(self, contract_session: AsyncSession) -> None:
        for waited_ms in (1_000, 2_000, 3_000, 4_000, 100_000):
            _, seats = pairing(subject(), subject(), waited_ms)
            await store(contract_session, *seats)

        health = await queue_health(contract_session)

        assert health.wait.sample == 5
        assert health.wait.p50_seconds == pytest.approx(3.0)
        assert health.wait.p95_seconds is not None
        assert health.wait.p95_seconds > health.wait.p50_seconds

    async def test_an_abandoned_wait_never_enters_the_pairing_distribution(
        self, contract_session: AsyncSession
    ) -> None:
        """§51. A censored wait is not a wait of zero, and mixing the two
        would make a product where people give up look fast."""
        _, seats = pairing(subject(), subject(), 10_000)
        person = subject()
        await store(
            contract_session, *seats, queue_join(person), queue_left(person, "expired", 600_000)
        )

        health = await queue_health(contract_session)

        assert health.wait.sample == 1
        assert health.wait.p50_seconds == pytest.approx(10.0)

    async def test_synthetic_and_other_environments_are_excluded(
        self, contract_session: AsyncSession
    ) -> None:
        person, ghost = subject(), subject()
        await store(
            contract_session,
            queue_join(person),
            queue_join(ghost, synthetic=True),
            queue_join(subject(), environment=Environment.STAGING),
        )

        assert (await queue_health(contract_session)).queue_joins == 1


class TestOfferHealth:
    """**MUTATION D targets these.**"""

    async def test_m9_over_the_three_outcomes(self, contract_session: AsyncSession) -> None:
        await store(
            contract_session,
            *(offer("both_accepted", hour=h) for h in range(1, 7)),
            *(offer("declined", hour=h) for h in range(7, 10)),
            offer("expired", hour=11),
        )

        health = await service(contract_session).offer_health(
            environment=PRODUCTION, since=DAY, until=DAY
        )

        assert (health.accepted, health.declined, health.expired) == (6, 3, 1)
        assert health.resolved == 10
        assert health.acceptance_rate == pytest.approx(0.6)

    async def test_an_expiry_is_not_a_decline(self, contract_session: AsyncSession) -> None:
        """§52. Somebody who never answered is not somebody who refused,
        and folding them would report indifference as rejection."""
        await store(contract_session, offer("expired"), offer("declined", hour=11))

        health = await service(contract_session).offer_health(
            environment=PRODUCTION, since=DAY, until=DAY
        )
        assert (health.declined, health.expired) == (1, 1)

    async def test_the_offer_grain_is_one_row_per_pairing(
        self, contract_session: AsyncSession
    ) -> None:
        """Two players share one offer resolution — the event is
        entity-level, so a two-seat pairing produces one row."""
        light, dark = subject(), subject()
        match_id, seats = started(light, dark)
        await store(contract_session, *seats, offer("both_accepted", match_id))

        health = await service(contract_session).offer_health(
            environment=PRODUCTION, since=DAY, until=DAY
        )
        assert health.resolved == 1


class TestGameHealth:
    """**MUTATION E and F target these.**"""

    async def test_a_two_player_match_counts_once(self, contract_session: AsyncSession) -> None:
        """The grain trap. `match_started` is projected per seat, so a
        `COUNT(*)` would report two matches and halve every rate — into a
        percentage that looks entirely reasonable."""
        match_id, seats = started(subject(), subject())
        await store(contract_session, *seats, completed(match_id, "resignation"))

        health = await game_health(contract_session)

        assert health.started == 1
        assert health.completed == 1
        assert health.completion_rate == pytest.approx(1.0)

    async def test_an_abort_is_excluded_from_both_sides(
        self, contract_session: AsyncSession
    ) -> None:
        """§32: 3 started, 1 aborted, 2 completed → 2/2, not 2/3."""
        finished = []
        for reason in ("resignation", "no_legal_moves", "abort"):
            match_id, seats = started(subject(), subject())
            finished.extend([*seats, completed(match_id, reason, hour=11)])
        await store(contract_session, *finished)

        health = await game_health(contract_session)

        assert (health.started, health.completed, health.aborted) == (3, 2, 1)
        assert health.completion_rate == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "reason",
        [
            "no_legal_moves",
            "all_pieces_captured",
            "resignation",
            "agreed_draw",
            "repetition",
            "move_limit",
            "flag",
            "flag_insufficient_material",
            "abandonment",
            "adjudication",
        ],
    )
    async def test_every_qualifying_reason_counts_as_completed(
        self, contract_session: AsyncSession, reason: str
    ) -> None:
        match_id, seats = started(subject(), subject())
        await store(contract_session, *seats, completed(match_id, reason))

        health = await game_health(contract_session)
        assert (health.completed, health.aborted) == (1, 0)

    async def test_a_started_match_with_no_completion_lowers_the_rate(
        self, contract_session: AsyncSession
    ) -> None:
        done_id, done_seats = started(subject(), subject())
        _, hanging = started(subject(), subject(), hour=12)
        await store(contract_session, *done_seats, *hanging, completed(done_id, "resignation"))

        health = await game_health(contract_session)

        assert (health.started, health.completed) == (2, 1)
        assert health.completion_rate == pytest.approx(0.5)

    async def test_the_termination_breakdown(self, contract_session: AsyncSession) -> None:
        events = []
        for reason, count in (("resignation", 3), ("flag", 2), ("agreed_draw", 1)):
            for index in range(count):
                match_id, seats = started(subject(), subject(), hour=1 + index)
                outcome = "draw" if reason == "agreed_draw" else "win"
                events.extend([*seats, completed(match_id, reason, outcome=outcome, hour=12)])
        await store(contract_session, *events)

        health = await game_health(contract_session)

        assert dict(health.termination_breakdown) == {
            "resignation": 3,
            "flag": 2,
            "agreed_draw": 1,
        }
        assert health.resignation_rate == pytest.approx(0.5)
        assert health.draw_rate == pytest.approx(1 / 6)
        # M13 folds abandonment and flag; there are two flags and no
        # abandonments here.
        assert health.abandonment_rate == pytest.approx(2 / 6)
        assert (health.abandonments, health.flags) == (0, 2)


class TestSegmentation:
    """**MUTATION I targets these** — §34's segment consistency."""

    async def test_a_rated_rate_has_a_rated_denominator(
        self, contract_session: AsyncSession
    ) -> None:
        rated_id, rated_seats = started(subject(), subject(), rated=True)
        casual_id, casual_seats = started(subject(), subject(), rated=False, hour=12)
        casual_two, casual_seats_two = started(subject(), subject(), rated=False, hour=13)
        await store(
            contract_session,
            *rated_seats,
            *casual_seats,
            *casual_seats_two,
            completed(rated_id, "resignation", rated=True),
            completed(casual_id, "resignation", rated=False, hour=14),
            completed(casual_two, "abort", rated=False, hour=15),
        )

        overall = await game_health(contract_session)
        rated = await game_health(contract_session, rated=True)
        casual = await game_health(contract_session, rated=False)

        assert (overall.started, overall.completed, overall.aborted) == (3, 2, 1)
        # Both halves segmented: one rated start, one rated completion.
        assert (rated.started, rated.completed, rated.aborted) == (1, 1, 0)
        assert rated.completion_rate == pytest.approx(1.0)
        # And the casual side keeps its own abort.
        assert (casual.started, casual.completed, casual.aborted) == (2, 1, 1)
        assert casual.completion_rate == pytest.approx(1.0)

    async def test_a_speed_class_segment_filters_both_halves(
        self, contract_session: AsyncSession
    ) -> None:
        """`match_started` carries no speed class today, so a segmented
        query narrows the completions and leaves the starts — which is why
        the assertion is on the completion counts rather than on the rate.
        Stated rather than hidden: the rate is unavailable per speed class
        until the additive field lands."""
        blitz_id, blitz_seats = started(subject(), subject())
        rapid_id, rapid_seats = started(subject(), subject(), hour=12)
        await store(
            contract_session,
            *blitz_seats,
            *rapid_seats,
            completed(blitz_id, "resignation", speed="blitz"),
            completed(rapid_id, "resignation", speed="rapid", hour=14),
        )

        blitz = await game_health(contract_session, speed_class=SpeedClass.BLITZ)
        rapid = await game_health(contract_session, speed_class=SpeedClass.RAPID)

        assert blitz.completed == 1
        assert rapid.completed == 1
        assert dict(blitz.termination_breakdown) == {"resignation": 1}


class TestDataQuality:
    async def test_a_completion_with_no_start(self, contract_session: AsyncSession) -> None:
        await store(contract_session, completed(str(uuid4()), "resignation"))

        quality = await service(contract_session).data_quality(
            environment=PRODUCTION, since=DAY, until=DAY
        )
        assert quality["completions_without_start"] == 1

    async def test_a_completion_before_its_start(self, contract_session: AsyncSession) -> None:
        match_id, seats = started(subject(), subject(), hour=15)
        await store(contract_session, *seats, completed(match_id, "resignation", hour=9))

        quality = await service(contract_session).data_quality(
            environment=PRODUCTION, since=DAY, until=DAY
        )
        assert quality["completed_before_start"] > 0

    async def test_an_unknown_resolution(self, contract_session: AsyncSession) -> None:
        await store(contract_session, offer("teleported"))

        quality = await service(contract_session).data_quality(
            environment=PRODUCTION, since=DAY, until=DAY
        )
        assert quality["unknown_resolutions"] == 1

    async def test_a_clean_day(self, contract_session: AsyncSession) -> None:
        match_id, seats = started(subject(), subject())
        await store(
            contract_session,
            *seats,
            completed(match_id, "resignation", hour=12),
            offer("both_accepted", match_id, hour=9),
        )

        quality = await service(contract_session).data_quality(
            environment=PRODUCTION, since=DAY, until=DAY
        )
        assert all(count == 0 for count in quality.values())


class TestProvenance:
    async def test_a_finished_day_is_mature_and_a_current_one_is_not(
        self, contract_session: AsyncSession
    ) -> None:
        finished = await queue_health(contract_session)
        current = await queue_health(contract_session, since=NOW.date(), until=NOW.date())

        assert finished.meta.maturity.value == "mature"
        assert current.meta.maturity.value == "partial"

    async def test_a_backwards_range_is_refused(self, contract_session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="ends before"):
            await queue_health(contract_session, since=DAY, until=DAY - timedelta(days=1))

    async def test_a_range_older_than_retention_is_truncated(
        self, contract_session: AsyncSession
    ) -> None:
        ancient = NOW.date() - timedelta(days=800)
        health = await queue_health(contract_session, since=ancient, until=NOW.date())
        assert health.meta.coverage.value == "truncated"
