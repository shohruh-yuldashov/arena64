"""What a funnel query answers — A64-027.3.

Typed results, and the types carry the decisions. Three of them are worth
naming here because a dashboard reading a bare number would get them wrong:

    a rate is a fraction, 0.0 to 1.0     never a percentage. One convention,
                                          and the one that composes — a rate
                                          of a rate is still a rate
    an undefined rate is `None`          not `0.0`. Nought conversions out
                                          of nought people is not "zero per
                                          cent"; it is a question with no
                                          answer, and a dashboard printing
                                          0% would show a failure that did
                                          not happen
    a duration is seconds                platform-wide, as `MetricsRecorder`
                                          already requires. Milliseconds is
                                          the other defensible choice and
                                          mixing them is the failure that
                                          matters

Every result carries its own provenance (`FunnelMeta`): which environment,
which window, whether the cohort is mature enough to read, and how much of
the history retention still covers. A number without those is a number
somebody will compare against another one that meant something else.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class Maturity(StrEnum):
    """Whether a cohort has had time to convert — §58.

    A cohort registered this morning has not had its activation window, so
    its activation rate is not low: it is **unfinished**. Reporting the two
    the same way is how a dashboard shows a cliff every day at midnight.
    """

    #: Every cohort in the range has had its full conversion window.
    MATURE = "mature"

    #: At least one cohort is still inside its window. The numbers can only
    #: rise.
    PARTIAL = "partial"


class Coverage(StrEnum):
    """Whether raw events still exist for the whole range — §57.

    Raw analytics is pruned at 400 days (A64-027.2 §53). A cohort older
    than that cannot be reconstructed, and a funnel over it would report a
    conversion of nought from a denominator that was deleted.
    """

    COMPLETE = "complete"

    #: Part of the requested range is older than the retention horizon.
    #: The result is reported with the range that survives, and this says
    #: so rather than the caller inferring it from a suspicious number.
    TRUNCATED = "truncated"


@dataclass(frozen=True, slots=True)
class FunnelMeta:
    """The provenance every funnel result carries."""

    environment: str
    include_synthetic: bool

    #: The cohort range actually measured, after retention truncation.
    cohort_from: date
    cohort_to: date

    #: What the caller asked for, before truncation. Equal to the pair
    #: above when `coverage` is `COMPLETE`.
    requested_from: date
    requested_to: date

    #: The conversion window each stage was allowed, in days.
    window_days: int

    maturity: Maturity
    coverage: Coverage
    generated_at: datetime

    #: Acquisition only. How many registrations happened in the range at
    #: all, against how many the identity stitch could attribute to a
    #: browser that was seen landing.
    #:
    #: `None` for the activation funnel, which needs no stitch — every one
    #: of its stages is keyed by a subject the server itself assigned.
    #:
    #: This exists because a conversion rate computed from the stitched
    #: number alone is a rate over a denominator nobody can see. Reporting
    #: both is the only honest way to publish the third stage at all
    #: (§14, and A64-027.1 §36's third limitation).
    registrations_in_range: int | None = None


@dataclass(frozen=True, slots=True)
class FunnelStage:
    """One step, and how many people reached it.

    `subjects` counts **people, not events** — a player who joined a queue
    twenty times is one person at the queue stage. §13 of the task, and the
    thing a naive `COUNT(*)` gets wrong while looking plausible.
    """

    stage: str
    subjects: int

    #: Reached this stage / reached the previous one. `None` for the first
    #: stage, and for any stage whose predecessor is empty.
    conversion_from_previous: float | None

    #: Reached this stage / reached the first one. `None` when the funnel
    #: starts empty.
    conversion_from_start: float | None

    #: Reached the previous stage and not this one. Never negative — the
    #: query is strictly nested, so a later stage is a subset of an earlier
    #: one by construction rather than by arithmetic.
    drop_off: int

    #: `drop_off` / the previous stage's `subjects`. `None` with no
    #: predecessor, or an empty one.
    drop_off_rate: float | None


@dataclass(frozen=True, slots=True)
class FunnelResult:
    stages: tuple[FunnelStage, ...]
    meta: FunnelMeta

    @property
    def overall_conversion(self) -> float | None:
        """The last stage over the first — §54.

        Kept separate from any step's `conversion_from_previous`, because
        the two answer different questions and a dashboard that conflated
        them would read a healthy last step as a healthy funnel.
        """
        if not self.stages:
            return None
        return self.stages[-1].conversion_from_start


@dataclass(frozen=True, slots=True)
class DurationSummary:
    """A distribution, never a mean.

    `p95` is a percentile from PostgreSQL's own `percentile_cont`, not an
    average of anything: §55, and the reason is that a mean over a skewed
    distribution describes nobody.

    `None` when the sample is empty. Zero would claim an instant conversion
    that nobody made.
    """

    #: How many subjects the distribution is over. Reported, because a p95
    #: of three people is a number to disbelieve.
    sample: int
    median_seconds: float | None
    p95_seconds: float | None


@dataclass(frozen=True, slots=True)
class ActivationSummary:
    """The activation funnel, plus the durations that explain it."""

    funnel: FunnelResult

    #: Registration to the first qualifying completed match — M20.
    #: **Survivor bias by construction**: it describes the people who
    #: activated, which A64-027.1 §36 states rather than leaves to be
    #: discovered.
    time_to_activation: DurationSummary

    #: Registration to verified — M5's companion.
    time_to_verify: DurationSummary


@dataclass(frozen=True, slots=True)
class DataQuality:
    """What the query refused to believe — §39, §40.

    Counts only. No subject key, no anonymous id, no event id: a diagnostic
    that named the rows would be a per-person export with none of the
    protections the event store has.

    A non-zero count here is not a query failure. It is a signal that
    something upstream produced an impossible journey, and the funnel
    excluded it rather than letting it inflate a stage.
    """

    #: A stage event whose instant precedes the registration it belongs to.
    #: Physically impossible; excluded from every stage.
    out_of_order_subjects: int

    #: A completion whose match has no start row for that player. Usually
    #: coverage — the match began before `match_started` was projected —
    #: rather than corruption.
    completions_without_start: int

    @property
    def is_clean(self) -> bool:
        return self.out_of_order_subjects == 0 and self.completions_without_start == 0
