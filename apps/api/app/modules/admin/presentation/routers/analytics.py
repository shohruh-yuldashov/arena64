"""The admin analytics API — A64-027.6.

    GET /api/v1/admin/analytics/overview
    GET /api/v1/admin/analytics/acquisition
    GET /api/v1/admin/analytics/retention
    GET /api/v1/admin/analytics/matchmaking
    GET /api/v1/admin/analytics/games

**Five aggregate reads, and no raw one.** There is no
`/admin/analytics/events`, no `/subjects`, and no way to ask this API about
a person — A64-027.1 §2's non-goal, enforced by the absence of an endpoint
rather than by a filter somebody could relax.

## Every handler is a mapper

The formulas live in `app.modules.analytics`'s services, which A64-027.3,
.4 and .5 tested against real PostgreSQL. Nothing here recomputes a rate,
and a handler that did would be a second definition of a canonical metric
in a layer with no tests for it.

## The range is required and capped

An administrator holds a token and can call these repeatedly. An unbounded
range would scan a year of events per request, which is a denial of service
with valid credentials. `MAX_RANGE_DAYS` is the cap and `400` is the wall —
raw retention, so a longer range could not be answered honestly anyway.

## Admin-only, and the guard is here rather than only in the console

`CurrentAdmin` on every handler. A route guard in `apps/admin` protects a
page; it does not protect an endpoint anybody can call with a normal
account's token.
"""

from datetime import date, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query

from app.api.deps import ClockDep
from app.config.environment import Environment
from app.core.exceptions import ValidationError
from app.modules.admin.presentation.dependencies import CurrentAdmin
from app.modules.admin.presentation.dependencies.analytics import (
    EngagementServiceDep,
    FunnelServiceDep,
    MatchmakingServiceDep,
)
from app.modules.admin.presentation.schemas.analytics import (
    AcquisitionResponse,
    ActivationResponse,
    ActivePlayersResponse,
    DurationResponse,
    EngagementResponse,
    FunnelStageResponse,
    GamesResponse,
    MatchmakingResponse,
    OverviewResponse,
    PeriodMeta,
    RetentionResponse,
    RetentionRowResponse,
    TerminationCount,
    WaitResponse,
)
from app.modules.analytics.application.read_models.engagement import (
    ActivePlayers,
    EngagementSummary,
    RetentionTable,
)
from app.modules.analytics.application.read_models.funnels import (
    ActivationSummary,
    DurationSummary,
    FunnelMeta,
    FunnelStage,
)
from app.modules.analytics.application.read_models.matchmaking import (
    GameHealth,
    OfferHealth,
    QueueHealth,
)

admin_analytics_router = APIRouter(prefix="/admin/analytics", tags=["admin"])

#: §8. Ninety days is the longest window any canonical metric is read over,
#: and it bounds the scan an administrator can ask for in one request.
MAX_RANGE_DAYS: Final = 90

#: The environment an admin console reports on. **Production only**, and
#: not a parameter: §11 — an environment selector on a production console
#: is a way to publish a laptop's numbers to somebody who will act on them.
REPORTED_ENVIRONMENT: Final = Environment.PRODUCTION.value


class DateRange:
    """A validated UTC window, resolved once per request.

    A dependency rather than two `Query` parameters checked in five
    handlers: the validation is the security control, and five copies is
    four places to forget it.

    **The clock is injected**, not `date.today()`. A process running in
    any timezone but UTC would otherwise compute a different "yesterday"
    from the one every read model uses, and the console would ask for a
    window the metrics do not describe — §10, and the platform's rule that
    time comes from `Clock` (AD-07).
    """

    def __init__(
        self,
        clock: ClockDep,
        start: Annotated[date | None, Query(description="First UTC day, inclusive.")] = None,
        end: Annotated[date | None, Query(description="Last UTC day, inclusive.")] = None,
    ) -> None:
        # §9. The default is the last thirty **complete** days, ending
        # yesterday: including today would put a partial day beside thirty
        # whole ones and make every morning look like a collapse.
        today = clock.now().date()
        self.end = end if end is not None else today - timedelta(days=1)
        self.start = start if start is not None else self.end - timedelta(days=29)

        if self.end < self.start:
            raise ValidationError("The range ends before it begins.")
        if (self.end - self.start).days + 1 > MAX_RANGE_DAYS:
            raise ValidationError(f"A range may span at most {MAX_RANGE_DAYS} days.")


DateRangeDep = Annotated[DateRange, Depends(DateRange)]


@admin_analytics_router.get(
    "/overview", response_model=OverviewResponse, summary="Product health at a glance"
)
async def read_overview(
    admin: CurrentAdmin,
    window: DateRangeDep,
    funnels: FunnelServiceDep,
    engagement: EngagementServiceDep,
    matchmaking: MatchmakingServiceDep,
) -> OverviewResponse:
    """Five sections, one call.

    Composed here for `admin/dashboard`'s reason: five round trips is five
    chances for a page to be half right, and each would carry its own
    period metadata for the same window.
    """
    activation = await funnels.activation(
        environment=REPORTED_ENVIRONMENT, since=window.start, until=window.end
    )
    players = await engagement.active_players(environment=REPORTED_ENVIRONMENT, as_of=window.end)
    week = await engagement.engagement(
        environment=REPORTED_ENVIRONMENT, week_start=window.end - timedelta(days=6)
    )
    queue = await matchmaking.queue_health(
        environment=REPORTED_ENVIRONMENT, since=window.start, until=window.end
    )
    offers = await matchmaking.offer_health(
        environment=REPORTED_ENVIRONMENT, since=window.start, until=window.end
    )
    games = await matchmaking.game_health(
        environment=REPORTED_ENVIRONMENT, since=window.start, until=window.end
    )

    return OverviewResponse(
        active_players=_players(players),
        activation=_activation(activation),
        matchmaking=_matchmaking(queue, offers),
        games=_games(games),
        engagement=_engagement(week),
        meta=_meta(activation.funnel.meta),
    )


@admin_analytics_router.get(
    "/acquisition", response_model=AcquisitionResponse, summary="The acquisition funnel"
)
async def read_acquisition(
    admin: CurrentAdmin, window: DateRangeDep, funnels: FunnelServiceDep
) -> AcquisitionResponse:
    result = await funnels.acquisition(
        environment=REPORTED_ENVIRONMENT, since=window.start, until=window.end
    )
    return AcquisitionResponse(
        stages=tuple(_stage(stage) for stage in result.stages),
        overall_conversion=result.overall_conversion,
        registrations_in_range=result.meta.registrations_in_range,
        meta=_meta(result.meta),
    )


@admin_analytics_router.get(
    "/retention", response_model=RetentionResponse, summary="Registration cohort retention"
)
async def read_retention(
    admin: CurrentAdmin, window: DateRangeDep, engagement: EngagementServiceDep
) -> RetentionResponse:
    table = await engagement.retention(
        environment=REPORTED_ENVIRONMENT, since=window.start, until=window.end
    )
    return _retention(table)


@admin_analytics_router.get(
    "/matchmaking", response_model=MatchmakingResponse, summary="Queue and offer health"
)
async def read_matchmaking(
    admin: CurrentAdmin, window: DateRangeDep, matchmaking: MatchmakingServiceDep
) -> MatchmakingResponse:
    queue = await matchmaking.queue_health(
        environment=REPORTED_ENVIRONMENT, since=window.start, until=window.end
    )
    offers = await matchmaking.offer_health(
        environment=REPORTED_ENVIRONMENT, since=window.start, until=window.end
    )
    return _matchmaking(queue, offers)


@admin_analytics_router.get("/games", response_model=GamesResponse, summary="Game health")
async def read_games(
    admin: CurrentAdmin, window: DateRangeDep, matchmaking: MatchmakingServiceDep
) -> GamesResponse:
    games = await matchmaking.game_health(
        environment=REPORTED_ENVIRONMENT, since=window.start, until=window.end
    )
    return _games(games)


# --- mappers, and nothing but mappers ---------------------------------------


def _meta(meta: FunnelMeta) -> PeriodMeta:
    return PeriodMeta(
        environment=meta.environment,
        include_synthetic=meta.include_synthetic,
        period_start=meta.cohort_from,
        period_end=meta.cohort_to,
        requested_start=meta.requested_from,
        requested_end=meta.requested_to,
        maturity=meta.maturity.value,
        coverage=meta.coverage.value,
        generated_at=meta.generated_at,
    )


def _stage(stage: FunnelStage) -> FunnelStageResponse:
    return FunnelStageResponse(
        stage=stage.stage,
        subjects=stage.subjects,
        conversion_from_previous=stage.conversion_from_previous,
        conversion_from_start=stage.conversion_from_start,
        drop_off=stage.drop_off,
        drop_off_rate=stage.drop_off_rate,
    )


def _duration(summary: DurationSummary) -> DurationResponse:
    return DurationResponse(
        sample=summary.sample,
        median_seconds=summary.median_seconds,
        p95_seconds=summary.p95_seconds,
    )


def _activation(summary: ActivationSummary) -> ActivationResponse:
    return ActivationResponse(
        stages=tuple(_stage(stage) for stage in summary.funnel.stages),
        overall_conversion=summary.funnel.overall_conversion,
        time_to_activation=_duration(summary.time_to_activation),
        time_to_verify=_duration(summary.time_to_verify),
        meta=_meta(summary.funnel.meta),
    )


def _players(players: ActivePlayers) -> ActivePlayersResponse:
    return ActivePlayersResponse(
        as_of=players.as_of,
        daily=players.daily,
        weekly=players.weekly,
        monthly=players.monthly,
        stickiness=players.stickiness,
    )


def _retention(table: RetentionTable) -> RetentionResponse:
    return RetentionResponse(
        rows=tuple(
            RetentionRowResponse(
                cohort_day=row.cohort_day,
                cohort=row.cohort,
                d1=row.d1,
                d7=row.d7,
                d30=row.d30,
                # Computed by the read model, not here: `rate` is where the
                # "unelapsed window is `None`, not nought" rule lives.
                d1_rate=row.rate(1),
                d7_rate=row.rate(7),
                d30_rate=row.rate(30),
            )
            for row in table.rows
        ),
        meta=_meta(table.meta),
    )


def _engagement(week: EngagementSummary) -> EngagementResponse:
    return EngagementResponse(
        week_start=week.week_start,
        week_end=week.week_end,
        active_players=week.active_players,
        match_starts=week.match_starts,
        matches_per_active_player=week.matches_per_active_player,
        median_matches_per_active_player=week.median_matches_per_active_player,
        tournament_entrants=week.tournament_entrants,
        tournament_participation=week.tournament_participation,
        friendships_created=week.friendships_created,
        challenges_sent=week.challenges_sent,
        challenges_accepted=week.challenges_accepted,
        challenges_declined=week.challenges_declined,
        challenges_expired=week.challenges_expired,
        challenges_cancelled=week.challenges_cancelled,
        challenge_acceptance=week.challenge_acceptance,
        meta=_meta(week.meta),
    )


def _matchmaking(queue: QueueHealth, offers: OfferHealth) -> MatchmakingResponse:
    return MatchmakingResponse(
        grain=queue.grain.value,
        queue_joins=queue.queue_joins,
        paired_attempts=queue.paired_attempts,
        abandoned_attempts=queue.abandoned_attempts,
        cancelled_attempts=queue.cancelled_attempts,
        expired_attempts=queue.expired_attempts,
        abandonment_rate=queue.abandonment_rate,
        match_found_rate=queue.match_found_rate,
        wait=WaitResponse(
            sample=queue.wait.sample,
            p50_seconds=queue.wait.p50_seconds,
            p95_seconds=queue.wait.p95_seconds,
        ),
        offers_accepted=offers.accepted,
        offers_declined=offers.declined,
        offers_expired=offers.expired,
        offers_resolved=offers.resolved,
        offer_acceptance=offers.acceptance_rate,
        meta=_meta(queue.meta),
    )


def _games(games: GameHealth) -> GamesResponse:
    return GamesResponse(
        grain=games.grain.value,
        started=games.started,
        completed=games.completed,
        aborted=games.aborted,
        completion_rate=games.completion_rate,
        resignation_rate=games.resignation_rate,
        draw_rate=games.draw_rate,
        abandonment_rate=games.abandonment_rate,
        rated_share=games.rated_share,
        resignations=games.resignations,
        draws=games.draws,
        abandonments=games.abandonments,
        flags=games.flags,
        rated_completions=games.rated_completions,
        termination_breakdown=tuple(
            TerminationCount(reason=reason, matches=count)
            for reason, count in games.termination_breakdown
        ),
        meta=_meta(games.meta),
    )


__all__ = ["MAX_RANGE_DAYS", "DateRange", "admin_analytics_router"]
