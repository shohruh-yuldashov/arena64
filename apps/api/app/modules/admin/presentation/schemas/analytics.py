"""The admin analytics API's response shapes — A64-027.6.

**Aggregate only, and the absences are the contract.** There is no
`player_id`, no `subject_key`, no `anonymous_id`, no `match_id`, no queue
ticket and no offer id anywhere below. Not filtered out — *undeclared*, so
a future edit that wanted one would have to add a field and argue for it.

An analytics dashboard that could name a person is a surveillance tool with
a chart on it, and A64-027.1 §2 states that as a non-goal rather than a
preference.

## `None` is not zero, everywhere

Every rate is `float | None`. `None` means the question has no answer —
an empty denominator, or a window that has not elapsed — and the console
renders it as a dash rather than as nought per cent. A64-027.4 §33: "a
partial D7 is always wrong and always looks like a decline."
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class _Aggregate(BaseModel):
    """Frozen and closed. `extra="forbid"` so a stray identifier cannot be
    passed through by a mapper that happened to have one."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PeriodMeta(_Aggregate):
    """What a number covers, and how far it can be trusted.

    Carried on every section because a figure without it gets compared
    against another that meant something else — the failure the whole
    maturity/coverage machinery exists to prevent.
    """

    environment: str
    include_synthetic: bool
    period_start: date
    period_end: date
    requested_start: date
    requested_end: date

    #: `mature` | `partial` — whether the window has fully elapsed.
    maturity: str

    #: `complete` | `truncated` — whether raw retention still covers it.
    coverage: str

    generated_at: datetime


class FunnelStageResponse(_Aggregate):
    stage: str
    subjects: int
    conversion_from_previous: float | None = None
    conversion_from_start: float | None = None
    drop_off: int
    drop_off_rate: float | None = None


class DurationResponse(_Aggregate):
    sample: int
    median_seconds: float | None = None
    p95_seconds: float | None = None


class AcquisitionResponse(_Aggregate):
    """F-A, with its coverage gap **on the response**.

    `registrations_in_range` is every registration in the window;
    `stages[-1].subjects` is the ones the identity stitch could attribute
    to a browser. The difference is the gap, and returning both is the only
    honest way to publish the third stage at all — A64-027.3 §64.
    """

    stages: tuple[FunnelStageResponse, ...]
    overall_conversion: float | None = None
    registrations_in_range: int | None = None
    meta: PeriodMeta


class ActivationResponse(_Aggregate):
    stages: tuple[FunnelStageResponse, ...]
    overall_conversion: float | None = None
    time_to_activation: DurationResponse
    time_to_verify: DurationResponse
    meta: PeriodMeta


class ActivePlayersResponse(_Aggregate):
    as_of: date
    daily: int
    weekly: int
    monthly: int
    stickiness: float | None = None


class RetentionRowResponse(_Aggregate):
    """One cohort. `d1`/`d7`/`d30` are `None` where the day has not
    arrived — never nought, which would be a decline that did not
    happen."""

    cohort_day: date
    cohort: int
    d1: int | None = None
    d7: int | None = None
    d30: int | None = None
    d1_rate: float | None = None
    d7_rate: float | None = None
    d30_rate: float | None = None


class EngagementResponse(_Aggregate):
    week_start: date
    week_end: date
    active_players: int
    match_starts: int
    matches_per_active_player: float | None = None
    median_matches_per_active_player: float | None = None
    tournament_entrants: int
    tournament_participation: float | None = None
    friendships_created: int
    challenges_sent: int
    challenges_accepted: int
    challenges_declined: int
    challenges_expired: int
    challenges_cancelled: int
    challenge_acceptance: float | None = None
    meta: PeriodMeta


class RetentionResponse(_Aggregate):
    rows: tuple[RetentionRowResponse, ...]
    meta: PeriodMeta


class WaitResponse(_Aggregate):
    """Seconds, and the sample counts **pairings** rather than seats."""

    sample: int
    p50_seconds: float | None = None
    p95_seconds: float | None = None


class MatchmakingResponse(_Aggregate):
    """M6 – M9 and M7b. `grain` is on the wire so a console cannot label a
    queue-attempt rate as a share of players."""

    grain: str
    queue_joins: int
    paired_attempts: int
    abandoned_attempts: int
    cancelled_attempts: int
    expired_attempts: int
    abandonment_rate: float | None = None
    match_found_rate: float | None = None
    wait: WaitResponse
    offers_accepted: int
    offers_declined: int
    offers_expired: int
    offers_resolved: int
    offer_acceptance: float | None = None
    meta: PeriodMeta


class TerminationCount(_Aggregate):
    reason: str
    matches: int


class GamesResponse(_Aggregate):
    """M10 – M14, at match grain.

    **No speed-class completion rate.** `match_started` carries no speed
    class, so a segmented denominator does not exist — A64-027.5 §89. The
    field is absent rather than nullable, because a nullable one invites a
    console to render a dash where a number will never come.
    """

    grain: str
    started: int
    completed: int
    aborted: int
    completion_rate: float | None = None
    resignation_rate: float | None = None
    draw_rate: float | None = None
    abandonment_rate: float | None = None
    rated_share: float | None = None
    resignations: int
    draws: int
    abandonments: int
    flags: int
    rated_completions: int
    termination_breakdown: tuple[TerminationCount, ...]
    meta: PeriodMeta


class OverviewResponse(_Aggregate):
    """The one call the overview page makes.

    Composed server-side for `admin/dashboard`'s reason: five sections
    rendered from five round trips is five chances for a page to be half
    right, and each would carry its own period metadata for the same
    window.
    """

    active_players: ActivePlayersResponse
    activation: ActivationResponse
    matchmaking: MatchmakingResponse
    games: GamesResponse
    engagement: EngagementResponse
    meta: PeriodMeta


class AnalyticsRangeQuery(_Aggregate):
    """The bounded window every endpoint takes — §8.

    A range is **required** and capped. `from=1970-01-01&to=9999-12-31`
    would scan the whole store on an endpoint an administrator can call
    repeatedly, which is a denial of service with a valid token.
    """

    start: date = Field(description="First UTC day, inclusive.")
    end: date = Field(description="Last UTC day, inclusive.")
