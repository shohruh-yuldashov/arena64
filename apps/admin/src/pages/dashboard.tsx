import { Link } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";

import { AUDIT_ACTION_LABELS } from "@/features/audit/vocabulary";
import {
  type AdminDashboard,
  type AdminDashboardActivity,
  fetchDashboard,
} from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { ErrorNotice } from "@/shared/ui/error-notice";
import { Icon } from "@/shared/ui/icon";
import { PageHeader } from "@/shared/ui/page-header";
import { Section } from "@/shared/ui/section";
import type { IconName } from "@/shared/ui/icon";
import { StatCard } from "@/shared/ui/stat-card";
import { ErrorState, LoadingSkeleton } from "@/shared/ui/states";

/**
 * The operator's first screen — A64-024.9.
 *
 * **A navigation and attention surface, not a metrics system.** Arena64
 * emits its metrics as structured log records — there is no Prometheus or
 * Grafana in this repository — and recomputing any of them here as a SQL
 * aggregate would be a second answer to the same question. What this page
 * answers is narrower and its own: is anything happening now, and is
 * anything waiting for a person.
 *
 * ## Every number is a link
 *
 * No card carries an action. A retry beside a failure count is one clicked
 * without reading which failure it was, and a restrict button beside a
 * restriction count is worse. Each figure links to the console that owns
 * the work, with the filter already applied where that console understands
 * one. The single control on this page is its own refresh, and a test
 * asserts that count inside `<main>` rather than trusting the reading.
 *
 * ## The composition — A64-027A.2 §18
 *
 * Header, then the six-figure snapshot, then the trail beside what needs a
 * person. A64-027A.1 stacked all three full width, which left six cards in
 * a four-column grid with two orphans and put a half-page of nothing to the
 * right of the activity list. The lower half is two columns now, which is
 * what uses the canvas rather than merely occupying it.
 *
 * ## No chart, and the reason — §21
 *
 * The dashboard's read model returns **current counts and nothing else**:
 * there is no time dimension in it. Every chart worth drawing here would be
 * interpolated from a single point, which §21 and §28 both forbid and which
 * would be the most convincing lie on the screen. The analytics endpoints
 * *do* carry real time-bounded aggregates, but composing them into this
 * page is a question about what the dashboard is for — it belongs to
 * A64-027A.4, not to a visual foundation task.
 *
 * ## What needs a person — A64-027A §7
 *
 * The six platform figures are context; the attention band is the reason an
 * operator opened the console at 3am. It is rendered above them when there
 * is anything in it, and it still renders when there is not — "all clear"
 * is an answer, and a section that vanishes is indistinguishable from one
 * that failed to load.
 *
 * The shortcut list A64-024.9 ended with is **gone**. It duplicated the
 * sidebar link for link, and the sidebar is now grouped and labelled, so
 * the list was a second navigation that could only ever drift from the
 * first.
 *
 * ## Freshness is explicit
 *
 * Fetched on mount and on an explicit refresh, and never polled: a page
 * that reissued six aggregate reads every few seconds would cost more than
 * everything it reports on. The server sends when it composed the numbers
 * and the page says so, because a figure with no age invites trust it has
 * not earned.
 *
 * A refresh that fails **keeps the numbers already on screen** and says the
 * refresh failed. Replacing known-good data with zeros would be the one
 * lie this page must not tell.
 */
/**
 * Which glyph an audited action wears — A64-027A.2 §22.
 *
 * Keyed on the action's own prefix rather than on a per-action map: the
 * trail is append-only and outlives this build, so an action nobody here
 * has heard of still gets the icon of the thing it happened to. `audit` is
 * the fallback, which is what an unrecognised privileged action *is*.
 */
function glyphFor(action: string): IconName {
  if (action.startsWith("admin.sanction")) return "moderation";
  if (action.startsWith("admin.role")) return "users";
  if (action.startsWith("tournament.")) return "tournaments";
  if (action.startsWith("notification.broadcast")) return "send";
  if (action.startsWith("notification.")) return "notifications";
  return "audit";
}

export function DashboardPage() {
  const { t, locale } = useTranslation();

  const [data, setData] = useState<AdminDashboard | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);

  const controller = useRef<AbortController | null>(null);

  const load = useCallback(async (isRefresh: boolean) => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;

    if (isRefresh) setRefreshing(true);
    setRefreshFailed(false);

    const outcome = await fetchDashboard(next.signal);
    if (next.signal.aborted) return;

    setRefreshing(false);
    if (outcome.status === "ok") {
      setData(outcome.value);
      setState("ready");
      return;
    }
    // On a refresh the existing numbers stay: they were true when they were
    // fetched, and the page says when that was.
    if (isRefresh) setRefreshFailed(true);
    else setState("error");
  }, []);

  useEffect(() => {
    void load(false);
    return () => controller.current?.abort();
  }, [load]);

  const when = (value: string) => new Date(value).toLocaleString(locale);

  const actionOf = (entry: AdminDashboardActivity) => {
    const label = AUDIT_ACTION_LABELS[entry.action];
    // An action this build cannot phrase keeps its identifier — the trail
    // outlives the console reading it.
    return label === undefined ? entry.action : t(label);
  };

  const actorOf = (entry: AdminDashboardActivity) => {
    if (entry.actor_id === null) return t("dashboard.activityOperator");
    return entry.actor_username ?? t("dashboard.activityUnknownAccount");
  };

  const attentionTotal =
    data === null
      ? 0
      : data.attention.restrictions_in_force + data.attention.push_deliveries_retry_exhausted;

  return (
    <>
      <PageHeader
        title={t("dashboard.title")}
        description={t("dashboard.lede")}
        actions={
          state === "ready" && data !== null ? (
            <>
              <span className="muted" style={{ fontSize: "var(--text-sm)" }}>
                {t("dashboard.updatedAt", { at: when(data.generated_at) })}
              </span>
              <button
                type="button"
                className="action"
                disabled={refreshing}
                onClick={() => void load(true)}
              >
                <Icon name="refresh" size={16} />
                {t(refreshing ? "dashboard.refreshing" : "dashboard.refresh")}
              </button>
            </>
          ) : undefined
        }
      />

      {state === "loading" && <LoadingSkeleton rows={4} label={t("dashboard.loading")} />}
      {state === "error" && (
        <ErrorState
          title={t("dashboard.error")}
          description={t("dashboard.errorHint")}
          onRetry={() => void load(false)}
        />
      )}

      {state === "ready" && data !== null && (
        <>
          {refreshFailed && <ErrorNotice message={t("dashboard.error")} />}

          <Section title={t("dashboard.sectionOverview")}>
            <ul className="kpi-grid">
              <li>
                <StatCard
                  label={t("dashboard.accountsDay")}
                  value={data.accounts.registered_last_day}
                  icon="users"
                  foot={<Link to="/users">{t("dashboard.viewUsers")}</Link>}
                />
              </li>
              <li>
                {/* Its own card rather than a footnote on the daily one: a
                    figure folded into a link label is a figure nobody
                    scans, and the week is the number that says whether the
                    day was normal. */}
                <StatCard
                  label={t("dashboard.accountsWeek")}
                  value={data.accounts.registered_last_week}
                  icon="users"
                  foot={<Link to="/users">{t("dashboard.viewUsers")}</Link>}
                />
              </li>
              <li>
                <StatCard
                  label={t("dashboard.matchesActive")}
                  value={data.matches.active}
                  icon="matches"
                  tone="success"
                  foot={
                    <Link to="/matches" search={{ status: "active" }}>
                      {t("dashboard.viewMatchesActive")}
                    </Link>
                  }
                />
              </li>
              <li>
                <StatCard
                  label={t("dashboard.matchesAwaiting")}
                  value={data.matches.awaiting_acceptance}
                  icon="matches"
                  tone="info"
                  foot={
                    <Link to="/matches" search={{ status: "awaiting_acceptance" }}>
                      {t("dashboard.viewMatchesAwaiting")}
                    </Link>
                  }
                />
              </li>
              <li>
                <StatCard
                  label={t("dashboard.tournamentsOpen")}
                  value={data.tournaments.registration_open}
                  icon="tournaments"
                  foot={
                    <Link to="/tournaments" search={{ status: "registration_open" }}>
                      {t("dashboard.viewTournamentsOpen")}
                    </Link>
                  }
                />
              </li>
              <li>
                <StatCard
                  label={t("dashboard.tournamentsRunning")}
                  value={data.tournaments.in_progress}
                  icon="tournaments"
                  tone="success"
                  foot={
                    <Link to="/tournaments" search={{ status: "in_progress" }}>
                      {t("dashboard.viewTournamentsRunning")}
                    </Link>
                  }
                />
              </li>
            </ul>
          </Section>

          <div className="dash-split">
            <section className="panel dash-split__attention">
              <div className="panel__head">
                <h3>
                  <Icon name="warning" size={16} />
                  {t("dashboard.sectionAttention")}
                </h3>
              </div>
              {attentionTotal === 0 ? (
                <p className="attention-clear" role="status">
                  <span className="attention-clear__glyph">
                    <Icon name="success" size={18} />
                  </span>
                  <span>
                    <strong>{t("dashboard.attentionClear")}</strong>
                    {t("dashboard.attentionClearHint")}
                  </span>
                </p>
              ) : (
                <ul className="attention-list">
                  {data.attention.restrictions_in_force > 0 && (
                    <li data-tone="warning">
                      <span className="attention__glyph" data-tone="warning">
                        <Icon name="moderation" size={16} />
                      </span>
                      <span className="attention__body">
                        <span className="attention__count">
                          {data.attention.restrictions_in_force}
                        </span>
                        <span className="attention__label">
                          {t("dashboard.attentionRestrictions")}
                        </span>
                        <Link to="/moderation">{t("dashboard.viewModeration")}</Link>
                      </span>
                    </li>
                  )}
                  {data.attention.push_deliveries_retry_exhausted > 0 && (
                    <li data-tone="danger">
                      <span className="attention__glyph" data-tone="danger">
                        <Icon name="notifications" size={16} />
                      </span>
                      <span className="attention__body">
                        <span className="attention__count">
                          {data.attention.push_deliveries_retry_exhausted}
                        </span>
                        <span className="attention__label">{t("dashboard.attentionPush")}</span>
                        <Link to="/notifications" search={{ failed: "true" }}>
                          {t("dashboard.viewNotifications")}
                        </Link>
                      </span>
                    </li>
                  )}
                </ul>
              )}
            </section>
            <section className="panel dash-split__activity">
              <div className="panel__head">
                <h3>
                  <Icon name="audit" size={16} />
                  {t("dashboard.sectionActivity")}
                </h3>
                <Link to="/audit">{t("dashboard.viewAudit")}</Link>
              </div>
              {data.recent_activity.length === 0 ? (
                <p className="muted panel__body" role="status">
                  {t("dashboard.activityEmpty")}
                </p>
              ) : (
                <ul className="timeline">
                  {data.recent_activity.map((entry) => (
                    <li key={entry.id}>
                      <span
                        className="timeline__glyph"
                        data-tone={entry.outcome === "succeeded" ? "primary" : "danger"}
                      >
                        <Icon name={glyphFor(entry.action)} size={15} />
                      </span>
                      <span className="timeline__text">
                        <strong>{actionOf(entry)}</strong>
                        <span>
                          {actorOf(entry)}
                          {" · "}
                          <time dateTime={entry.created_at}>{when(entry.created_at)}</time>
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </>
      )}
    </>
  );
}
