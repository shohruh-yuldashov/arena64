"""Engagement and retention results — A64-027.4.

Part III's conventions apply unchanged: a rate is a fraction, an undefined
rate is `None`, a duration is seconds, and every result carries the
provenance that makes it comparable.

What is new here is **maturity per cohort row** rather than per result. A
retention table shows many cohorts at once and they mature at different
times: a January cohort has its D30 and a cohort from three days ago does
not. Reporting one maturity for the table would either hide a finished
number or publish an unfinished one, so each row carries its own.
"""

from dataclasses import dataclass
from datetime import date

from app.modules.analytics.application.read_models.funnels import FunnelMeta


@dataclass(frozen=True, slots=True)
class ActivePlayers:
    """DAU, WAU and MAU over A64-027.1 §30's definition.

    Three windows ending on the same day, so they are directly comparable —
    `daily / monthly` is the stickiness ratio a dashboard will want and it
    is only meaningful when the three share an end.
    """

    #: The last day of every window. All three look backwards from here.
    as_of: date

    daily: int
    weekly: int
    monthly: int

    @property
    def stickiness(self) -> float | None:
        """DAU / MAU — how much of a month's audience shows up on a day.

        `None` when nobody was active in the month: nought out of nought is
        a question with no answer, and `0.0` would read as an audience that
        never returns rather than one that does not exist yet.
        """
        if self.monthly <= 0:
            return None
        return self.daily / self.monthly


@dataclass(frozen=True, slots=True)
class RetentionRow:
    """One registration cohort, and how much of it came back.

    `d1`, `d7` and `d30` are counts of people from `cohort` who met §30's
    active-player definition **on that exact calendar day** — not "within
    that many days". A64-027.1 §33 froze the distinction, and it is the one
    a reader assumes wrongly.
    """

    cohort_day: date
    cohort: int

    #: `None` where the window has not elapsed. **Not zero** — a cohort that
    #: registered yesterday has not failed its D7; it has not had one.
    d1: int | None
    d7: int | None
    d30: int | None

    def rate(self, day: int) -> float | None:
        """The retained fraction for D1, D7 or D30.

        `None` for an unelapsed window and for an empty cohort, which are
        different reasons for the same answer: neither is a number.
        """
        retained = {1: self.d1, 7: self.d7, 30: self.d30}[day]
        if retained is None or self.cohort <= 0:
            return None
        return retained / self.cohort


@dataclass(frozen=True, slots=True)
class RetentionTable:
    rows: tuple[RetentionRow, ...]
    meta: FunnelMeta

    def mature_rows(self, day: int) -> tuple[RetentionRow, ...]:
        """The cohorts whose window for `day` has elapsed.

        A64-027.1 §33: "a cohort is reported only once its window has fully
        elapsed. A partial D7 is always wrong and always looks like a
        decline." This is how a caller averages without averaging in the
        cohorts that have not finished.
        """
        return tuple(row for row in self.rows if row.rate(day) is not None)


@dataclass(frozen=True, slots=True)
class EngagementSummary:
    """The weekly engagement metrics — M15, M16, M17, M22.

    One window, one result, because every one of them is "per active
    player, this week" and computing them separately would let three
    dashboards disagree about which week they meant.
    """

    week_start: date
    week_end: date
    meta: FunnelMeta

    #: M18 over the week, and the denominator of the two rates below.
    active_players: int

    #: M22. A **mean**, and A64-027.1 §29 says to read it beside the median
    #: because the distribution is skewed — a handful of people play a lot.
    match_starts: int
    matches_per_active_player: float | None
    median_matches_per_active_player: float | None

    #: M15. Distinct actors who entered a tournament, over active players.
    tournament_entrants: int
    tournament_participation: float | None

    #: M16. A count, not a rate — the graph either grew or it did not.
    friendships_created: int

    #: M17, and the three refusals are separate on purpose: A64-027.1 §29
    #: says expiry and decline are reported apart rather than merged into
    #: failure, because they are different product problems.
    challenges_sent: int
    challenges_accepted: int
    challenges_declined: int
    challenges_expired: int
    challenges_cancelled: int

    @property
    def challenge_acceptance(self) -> float | None:
        if self.challenges_sent <= 0:
            return None
        return self.challenges_accepted / self.challenges_sent
