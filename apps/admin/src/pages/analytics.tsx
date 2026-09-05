import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import {
  type AnalyticsAcquisition,
  type AnalyticsOverview,
  type AnalyticsRetention,
  fetchAnalyticsAcquisition,
  fetchAnalyticsOverview,
  fetchAnalyticsRetention,
} from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { ErrorNotice } from "@/shared/ui/error-notice";
import {
  Metric,
  PeriodBadge,
  formatCount,
  formatDuration,
  formatRate,
} from "@/shared/ui/metric";
import {
  CohortCell,
  DistributionBar,
  FlowStep,
  FunnelStage,
} from "@/features/analytics/charts";
import { useVocab } from "@/features/vocabulary";
import { Icon, type IconName } from "@/shared/ui/icon";
import { PageHeader } from "@/shared/ui/page-header";

/**
 * Product analytics — A64-027.6, and the last screen of the A64-027 epic.
 *
 * ## It renders; it does not compute
 *
 * Every figure below arrives from a canonical read model that A64-027.3 to
 * .5 tested against real PostgreSQL. There is no arithmetic on this page
 * beyond picking a bar's width: a `completed / started` here would be a
 * second definition of M10 without M10's abort semantics, in a layer with
 * no tests for it.
 *
 * ## Three requests, not twelve
 *
 * The overview is composed server-side, so five sections cost one call.
 * Retention and acquisition are separate because each answers a different
 * question about a different population — cohorts, and browsers that have
 * not registered yet. Folding them in would make one endpoint answer three
 * questions about three ranges.
 *
 * Fetched on mount, on a range change, and on an explicit refresh. **Never
 * polled**: product analytics is not a trading terminal, and a page that
 * reissued six aggregate reads a minute would cost more than everything it
 * reports on. The dashboard beside it made the same choice for the same
 * reason.
 *
 * ## A failed refresh keeps the numbers
 *
 * Replacing known-good figures with zeros is the one lie this page must
 * not tell — `pages/dashboard` states it and it is more important here,
 * because zero is a *meaningful value* for most of these metrics.
 *
 * ## No trend arrows
 *
 * §15. The backend computes no comparable previous period with matching
 * completeness semantics, so there is nothing honest to point an arrow at.
 * A dashboard without decorative mathematics is better than one with it.
 */

const RANGES = [7, 30, 90] as const;
type RangeDays = (typeof RANGES)[number];

interface Loaded {
  overview: AnalyticsOverview;
  retention: AnalyticsRetention;
  acquisition: AnalyticsAcquisition;
}

function windowFor(days: RangeDays): { start: string; end: string } {
  // Ends **yesterday**, matching the server's default: a partial day beside
  // whole ones makes every morning look like a collapse.
  const end = new Date(Date.now() - 86_400_000);
  const start = new Date(end.getTime() - (days - 1) * 86_400_000);
  const iso = (value: Date) => value.toISOString().slice(0, 10);
  return { start: iso(start), end: iso(end) };
}

export function AnalyticsPage() {
  const { t, locale } = useTranslation();

  const [days, setDays] = useState<RangeDays>(30);
  const [data, setData] = useState<Loaded | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);

  const controller = useRef<AbortController | null>(null);

  const load = useCallback(async (range: RangeDays, isRefresh: boolean) => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;

    if (isRefresh) setRefreshing(true);
    setRefreshFailed(false);

    const window = windowFor(range);
    // Concurrent, because they are independent reads on one request's
    // session pool and serialising them would double the page's latency
    // for nothing.
    const [overview, retention, acquisition] = await Promise.all([
      fetchAnalyticsOverview(window, next.signal),
      fetchAnalyticsRetention(window, next.signal),
      fetchAnalyticsAcquisition(window, next.signal),
    ]);

    if (next.signal.aborted) return;
    setRefreshing(false);

    if (overview.status !== "ok" || retention.status !== "ok" || acquisition.status !== "ok") {
      if (isRefresh) setRefreshFailed(true);
      else setState("error");
      return;
    }

    setData({
      overview: overview.value,
      retention: retention.value,
      acquisition: acquisition.value,
    });
    setState("ready");
  }, []);

  useEffect(() => {
    void load(days, false);
    return () => controller.current?.abort();
  }, [days, load]);

  return (
    <section>
      <PageHeader title={t("analytics.title")} description={t("analytics.lede")} />

      <div className="dashboard-freshness">
        <fieldset className="range">
          <legend className="sr-only">{t("analytics.rangeLabel")}</legend>
          {RANGES.map((option) => (
            <button
              key={option}
              type="button"
              className="action"
              aria-pressed={days === option}
              onClick={() => setDays(option)}
            >
              {t(`analytics.range${String(option)}` as "analytics.range7")}
            </button>
          ))}
        </fieldset>

        <button
          type="button"
          className="action"
          onClick={() => void load(days, true)}
          disabled={refreshing || state === "loading"}
        >
          {refreshing ? t("analytics.refreshing") : t("analytics.refresh")}
        </button>

        <span className="muted">{t("analytics.productionOnly")}</span>
        {data !== null && (
          <span className="muted">
            {t("analytics.updatedAt", {
              at: new Date(data.overview.meta.generated_at).toLocaleString(locale),
            })}
          </span>
        )}
      </div>

      {refreshFailed && <ErrorNotice message={t("analytics.error")} />}

      {state === "loading" && <p className="muted">{t("analytics.loading")}</p>}

      {state === "error" && (
        <>
          <ErrorNotice message={t("analytics.error")} />
          <button type="button" className="action" onClick={() => void load(days, false)}>
            {t("analytics.retry")}
          </button>
        </>
      )}

      {state === "ready" && data !== null && <Sections data={data} locale={locale} />}
    </section>
  );
}

/**
 * A band heading, matching the rest of the console — A64-027A §6.
 *
 * The analytics page predates `Section` and its bands are not wrappable
 * without restructuring the page: several of them are a heading followed by
 * two sibling elements that share a grid. So the heading alone adopts the
 * shared treatment, which is what the reader actually sees.
 */
/**
 * A named group of related figures — A64-027A.4 §7.
 *
 * The overview was ten `Metric` cards in one grid, which asks the reader to
 * work out which four are about players and which three are about the
 * queue. Naming the group does that work once.
 */
function MetricGroup({
  title,
  icon,
  children,
}: {
  title: string;
  icon: IconName;
  children: ReactNode;
}) {
  return (
    <section className="metric-group">
      <h3 className="metric-group__title">
        <Icon name={icon} size={15} />
        {title}
      </h3>
      <dl className="metric-group__body">{children}</dl>
    </section>
  );
}

function SectionHeading({ children, level = 3 }: { children: ReactNode; level?: 3 | 4 }) {
  const Tag = level === 3 ? "h3" : "h4";
  return (
    <div className="section__head">
      <Tag>{children}</Tag>
    </div>
  );
}

function Sections({ data, locale }: { data: Loaded; locale: string }) {
  const { t } = useTranslation();
  const { overview, retention, acquisition } = data;
  const { active_players: players, activation, matchmaking, games, engagement } = overview;

  return (
    <>
      <PeriodBadge meta={overview.meta} />

      {/* Four groups rather than ten identical cards in one grid — §7.
          A dashboard of undifferentiated tiles makes the reader do the
          grouping; naming the groups is what turns a wall of figures into
          an answer to "how is the product doing". */}
      <div className="metric-groups">
        <MetricGroup title={t("analytics.groupActivity")} icon="users">
          <Metric
            label={t("analytics.dau")}
            value={formatCount(players.daily, locale)}
            hint={t("analytics.dauHint")}
          />
          <Metric label={t("analytics.wau")} value={formatCount(players.weekly, locale)} />
          <Metric label={t("analytics.mau")} value={formatCount(players.monthly, locale)} />
          <Metric
            label={t("analytics.stickiness")}
            value={formatRate(players.stickiness, locale)}
            hint={t("analytics.stickinessHint")}
          />
        </MetricGroup>

        <MetricGroup title={t("analytics.groupActivation")} icon="success">
          <Metric
            label={t("analytics.activationRate")}
            value={formatRate(activation.overall_conversion, locale)}
            hint={t("analytics.activationHint")}
          />
          <Metric
            label={t("analytics.timeToActivation")}
            value={formatDuration(activation.time_to_activation.median_seconds, locale)}
            detail={t("analytics.sample", {
              count: String(activation.time_to_activation.sample),
            })}
          />
        </MetricGroup>

        <MetricGroup title={t("analytics.groupMatchmaking")} icon="matches">
          <Metric
            label={t("analytics.queueWait")}
            value={formatDuration(matchmaking.wait.p50_seconds, locale)}
            hint={t("analytics.queueWaitHint")}
            detail={`${t("analytics.p95")} ${formatDuration(matchmaking.wait.p95_seconds, locale) ?? t("analytics.unavailable")}`}
          />
          <Metric
            label={t("analytics.queueAbandonment")}
            value={formatRate(matchmaking.abandonment_rate, locale)}
            hint={t("analytics.queueAbandonmentHint")}
          />
          <Metric
            label={t("analytics.offerAcceptance")}
            value={formatRate(matchmaking.offer_acceptance, locale)}
            hint={t("analytics.offerAcceptanceHint")}
          />
        </MetricGroup>

        <MetricGroup title={t("analytics.groupGames")} icon="tournaments">
          <Metric
            label={t("analytics.completionRate")}
            value={formatRate(games.completion_rate, locale)}
            hint={t("analytics.completionHint")}
          />
          <Metric
            label={t("analytics.ratedShare")}
            value={formatRate(games.rated_share, locale)}
          />
        </MetricGroup>
      </div>

      <SectionHeading>{t("analytics.sectionAcquisition")}</SectionHeading>
      <Acquisition acquisition={acquisition} locale={locale} />

      <SectionHeading>{t("analytics.sectionActivation")}</SectionHeading>
      <Funnel stages={activation.stages} locale={locale} />
      <dl className="kpi-grid">
        <Metric
          label={t("analytics.timeToVerify")}
          value={formatDuration(activation.time_to_verify.median_seconds, locale)}
          detail={t("analytics.sample", { count: String(activation.time_to_verify.sample) })}
        />
        <Metric
          label={`${t("analytics.timeToActivation")} · ${t("analytics.p95")}`}
          value={formatDuration(activation.time_to_activation.p95_seconds, locale)}
        />
      </dl>

      <SectionHeading>{t("analytics.sectionEngagement")}</SectionHeading>
      <dl className="kpi-grid">
        <Metric
          label={`${t("analytics.matchesPerPlayer")} · ${t("analytics.mean")}`}
          value={
            engagement.matches_per_active_player === null
              ? null
              : new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(
                  engagement.matches_per_active_player,
                )
          }
          detail={`${t("analytics.median")} ${
            engagement.median_matches_per_active_player === null
              ? t("analytics.unavailable")
              : new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(
                  engagement.median_matches_per_active_player,
                )
          }`}
        />
        <Metric
          label={t("analytics.tournamentParticipation")}
          value={formatRate(engagement.tournament_participation, locale)}
        />
        <Metric
          label={t("analytics.friendships")}
          value={formatCount(engagement.friendships_created, locale)}
        />
        <Metric
          label={t("analytics.challengeAcceptance")}
          value={formatRate(engagement.challenge_acceptance, locale)}
          detail={t("analytics.challengeBreakdown", {
            sent: String(engagement.challenges_sent),
            accepted: String(engagement.challenges_accepted),
            declined: String(engagement.challenges_declined),
            expired: String(engagement.challenges_expired),
          })}
        />
      </dl>

      <SectionHeading>{t("analytics.sectionRetention")}</SectionHeading>
      <PeriodBadge meta={retention.meta} />
      <RetentionTable rows={retention.rows} locale={locale} />

      <SectionHeading>{t("analytics.sectionMatchmaking")}</SectionHeading>
      {/* The queue's shape, in the order a ticket travels through it. The
          two rates between the steps are the **server's** — recomputing
          `match_found_rate` from the counts beside it would be a second
          definition of a product metric (§37). */}
      <div className="flow">
        <FlowStep
          label={t("analytics.queueJoins")}
          value={formatCount(matchmaking.queue_joins, locale)}
          rate={formatRate(matchmaking.match_found_rate, locale)}
        />
        <FlowStep
          label={t("analytics.queuePaired")}
          value={formatCount(matchmaking.paired_attempts, locale)}
          rate={formatRate(matchmaking.offer_acceptance, locale)}
        />
        <FlowStep
          label={t("analytics.offersAccepted")}
          value={formatCount(matchmaking.offers_accepted, locale)}
          last
        />
      </div>
      <p className="muted flow__note">{t("analytics.queueGrainHint")}</p>

      <dl className="kpi-grid">
        <Metric
          label={t("analytics.queueAbandonment")}
          value={formatRate(matchmaking.abandonment_rate, locale)}
          hint={t("analytics.queueAbandonmentHint")}
          detail={t("analytics.attemptBreakdown", {
            abandoned: String(matchmaking.abandoned_attempts),
            cancelled: String(matchmaking.cancelled_attempts),
            expired: String(matchmaking.expired_attempts),
          })}
        />
        <Metric
          label={t("analytics.offerAcceptance")}
          value={formatRate(matchmaking.offer_acceptance, locale)}
          detail={t("analytics.offerBreakdown", {
            accepted: String(matchmaking.offers_accepted),
            declined: String(matchmaking.offers_declined),
            expired: String(matchmaking.offers_expired),
          })}
        />
        <Metric
          label={t("analytics.queueWait")}
          value={formatDuration(matchmaking.wait.p50_seconds, locale)}
          hint={t("analytics.queueWaitHint")}
          detail={`${t("analytics.p95")} ${formatDuration(matchmaking.wait.p95_seconds, locale) ?? t("analytics.unavailable")}`}
        />
      </dl>

      <SectionHeading>{t("analytics.sectionGames")}</SectionHeading>
      <dl className="kpi-grid">
        <Metric
          label={t("analytics.matchesStarted")}
          value={formatCount(games.started, locale)}
        />
        <Metric
          label={t("analytics.matchesCompleted")}
          value={formatCount(games.completed, locale)}
        />
        <Metric
          label={t("analytics.matchesAborted")}
          value={formatCount(games.aborted, locale)}
        />
        <Metric
          label={t("analytics.resignationRate")}
          value={formatRate(games.resignation_rate, locale)}
        />
        <Metric label={t("analytics.drawRate")} value={formatRate(games.draw_rate, locale)} />
        <Metric
          label={t("analytics.abandonmentRate")}
          value={formatRate(games.abandonment_rate, locale)}
        />
      </dl>

      <SectionHeading level={4}>{t("analytics.termination")}</SectionHeading>
      <TerminationBreakdown breakdown={games.termination_breakdown} locale={locale} />
    </>
  );
}

/**
 * The acquisition funnel, and the reason not to trust its top.
 *
 * `user_registered` carries no `anonymous_id`, so the join from browser to
 * account is made at query time over the rows that happen to carry both —
 * and coverage of that is currently near zero. A funnel drawn without
 * saying so reads as a catastrophic landing page rather than as a
 * measurement gap, so the registrations the period actually saw are stated
 * beside it. A64-027.3 §72; the limitation is open, not solved.
 */
function Acquisition({
  acquisition,
  locale,
}: {
  acquisition: AnalyticsAcquisition;
  locale: string;
}) {
  const { t } = useTranslation();

  return (
    <>
      <PeriodBadge meta={acquisition.meta} />
      <p className="badges">
        {/* The badge names the problem; the paragraph below states it in
            full. Repeating the explanation inside the badge as `sr-only`
            would make a screen reader read the same sentence twice. */}
        <span className="badge" data-kind="partial">
          {t("analytics.acquisitionLimited")}
        </span>
      </p>
      <p className="muted">
        {t("analytics.acquisitionLimitedHint", {
          total:
            acquisition.registrations_in_range === null
              ? t("analytics.unavailable")
              : formatCount(acquisition.registrations_in_range, locale),
        })}
      </p>
      <Funnel stages={acquisition.stages} locale={locale} />
    </>
  );
}

function Funnel({
  stages,
  locale,
}: {
  stages: AnalyticsOverview["activation"]["stages"];
  locale: string;
}) {
  const { t } = useTranslation();
  const head = stages[0];
  if (head === undefined || head.subjects === 0) {
    return <p className="muted">{t("analytics.empty")}</p>;
  }

  const first = head.subjects;

  return (
    <>
      {/* The shape, drawn from the population of the first stage — which is
          what makes a funnel a funnel rather than four unrelated bars. */}
      <div className="funnel">
        {stages.map((stage) => (
          <FunnelStage
            key={stage.stage}
            label={t(`analytics.stageName.${stage.stage}` as "analytics.stageName.activated")}
            subjects={stage.subjects}
            first={first}
            fromPrevious={formatRate(stage.conversion_from_previous, locale)}
            fromStart={formatRate(stage.conversion_from_start, locale)}
            dropOff={stage.drop_off}
            locale={locale}
          />
        ))}
      </div>

      {/* The same numbers as a table. §19: a chart whose data lives only in
          its geometry is a chart half the readers cannot use, and this is
          also what a screen reader gets. */}
      <details className="chart-table">
        <summary>{t("analytics.showTable")}</summary>
        <div className="table-scroll">
          <table className="analytics-table">
            <caption className="sr-only">{t("analytics.sectionActivation")}</caption>
            <thead>
              <tr>
                <th scope="col">{t("analytics.stage")}</th>
                <th scope="col">{t("analytics.subjects")}</th>
                <th scope="col">{t("analytics.fromPrevious")}</th>
                <th scope="col">{t("analytics.fromStart")}</th>
                <th scope="col">{t("analytics.dropOff")}</th>
              </tr>
            </thead>
            <tbody>
              {stages.map((stage) => (
                <tr key={stage.stage}>
                  <th scope="row">
                    {t(`analytics.stageName.${stage.stage}` as "analytics.stageName.activated")}
                  </th>
                  <td>{formatCount(stage.subjects, locale)}</td>
                  <td>
                    {formatRate(stage.conversion_from_previous, locale) ??
                      t("analytics.unavailable")}
                  </td>
                  <td>
                    {formatRate(stage.conversion_from_start, locale) ??
                      t("analytics.unavailable")}
                  </td>
                  <td>{formatCount(stage.drop_off, locale)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </>
  );
}

function RetentionTable({
  rows,
  locale,
}: {
  rows: AnalyticsRetention["rows"];
  locale: string;
}) {
  const { t } = useTranslation();
  if (rows.length === 0) return <p className="muted">{t("analytics.empty")}</p>;

  return (
    <div className="table-scroll">
      <table className="analytics-table cohort">
        <caption className="sr-only">{t("analytics.sectionRetention")}</caption>
        <thead>
          <tr>
            <th scope="col">{t("analytics.cohort")}</th>
            <th scope="col">{t("analytics.cohortSize")}</th>
            <th scope="col">D1</th>
            <th scope="col">D7</th>
            <th scope="col">D30</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.cohort_day}>
              <th scope="row">{row.cohort_day}</th>
              <td>{formatCount(row.cohort, locale)}</td>
              {/* The tint is the rate the server returned; `null` carries no
                  tint at all, because a day that has not arrived is not a
                  cohort that churned. */}
              <CohortCell rate={row.d1_rate} label={formatRate(row.d1_rate, locale)} />
              <CohortCell rate={row.d7_rate} label={formatRate(row.d7_rate, locale)} />
              <CohortCell rate={row.d30_rate} label={formatRate(row.d30_rate, locale)} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TerminationBreakdown({
  breakdown,
  locale,
}: {
  breakdown: AnalyticsOverview["games"]["termination_breakdown"];
  locale: string;
}) {
  const { t } = useTranslation();
  const vocab = useVocab();
  if (breakdown.length === 0) return <p className="muted">{t("analytics.empty")}</p>;

  const max = Math.max(...breakdown.map((item) => item.matches));
  const total = breakdown.reduce((sum, item) => sum + item.matches, 0);

  return (
    <div className="dists">
      {breakdown.map((item) => (
        <DistributionBar
          key={item.reason}
          label={vocab("termination", item.reason)}
          value={item.matches}
          max={max}
          // The count, and its share of the reasons shown. Both the
          // numerator and the denominator are the server's own counts,
          // summed here for display — it is not a product metric and does
          // not enter one.
          display={`${formatCount(item.matches, locale)}${
            total > 0 ? ` · ${formatRate(item.matches / total, locale) ?? ""}` : ""
          }`}
          tone={TERMINATION_TONES[item.reason]}
        />
      ))}
    </div>
  );
}

/**
 * How each ending reads at a glance.
 *
 * Semantic rather than decorative: an abort or an abandonment is a game
 * nobody finished, a resignation or a flag is a game that ended properly.
 * **No reason is regrouped** — §17 — the bars stay one per canonical value.
 */
const TERMINATION_TONES: Record<string, "primary" | "success" | "warning" | "danger"> = {
  no_legal_moves: "success",
  all_pieces_captured: "success",
  agreed_draw: "success",
  repetition: "success",
  resignation: "primary",
  flag: "primary",
  flag_insufficient_material: "primary",
  move_limit: "primary",
  abort: "warning",
  abandonment: "danger",
  adjudication: "warning",
};
