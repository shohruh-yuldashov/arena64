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
import { PageHeader } from "@/shared/ui/page-header";

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
 * one.
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
      <PageHeader title={t("dashboard.title")} description={t("dashboard.lede")} />

      {state === "loading" && <p role="status">{t("dashboard.loading")}</p>}
      {state === "error" && <ErrorNotice message={t("dashboard.error")} />}

      {state === "ready" && data !== null && (
        <>
          <p className="dashboard-freshness">
            <button
              type="button"
              className="action"
              disabled={refreshing}
              onClick={() => void load(true)}
            >
              {t(refreshing ? "dashboard.refreshing" : "dashboard.refresh")}
            </button>
            <span className="muted">
              {t("dashboard.updatedAt", { at: when(data.generated_at) })}
            </span>
          </p>

          {refreshFailed && <ErrorNotice message={t("dashboard.error")} />}

          <section>
            <h3>{t("dashboard.sectionOverview")}</h3>
            <ul className="kpi-grid">
              <li>
                <Link to="/users">{t("dashboard.accounts")}</Link>
                <dl>
                  <dt>{t("dashboard.accountsDay")}</dt>
                  <dd className="kpi-value">{data.accounts.registered_last_day}</dd>
                  <dt>{t("dashboard.accountsWeek")}</dt>
                  <dd className="kpi-value">{data.accounts.registered_last_week}</dd>
                </dl>
              </li>
              <li>
                <Link to="/matches" search={{ status: "active" }}>
                  {t("dashboard.matches")}
                </Link>
                <dl>
                  <dt>{t("dashboard.matchesActive")}</dt>
                  <dd className="kpi-value">{data.matches.active}</dd>
                  <dt>{t("dashboard.matchesAwaiting")}</dt>
                  <dd className="kpi-value">{data.matches.awaiting_acceptance}</dd>
                </dl>
              </li>
              <li>
                <Link to="/tournaments" search={{ status: "in_progress" }}>
                  {t("dashboard.tournaments")}
                </Link>
                <dl>
                  <dt>{t("dashboard.tournamentsOpen")}</dt>
                  <dd className="kpi-value">{data.tournaments.registration_open}</dd>
                  <dt>{t("dashboard.tournamentsRunning")}</dt>
                  <dd className="kpi-value">{data.tournaments.in_progress}</dd>
                </dl>
              </li>
            </ul>
          </section>

          <section>
            <h3>{t("dashboard.sectionAttention")}</h3>
            {/* Zero is a real answer and is rendered as one — the list still
                appears, saying nothing needs attention, rather than
                vanishing and leaving the operator to wonder whether it
                loaded. */}
            {attentionTotal === 0 ? (
              <>
                <p role="status">{t("dashboard.attentionClear")}</p>
                <p className="muted">{t("dashboard.attentionClearHint")}</p>
              </>
            ) : (
              <ul className="attention-list">
                {data.attention.restrictions_in_force > 0 && (
                  <li>
                    <span className="kpi-value">{data.attention.restrictions_in_force}</span>{" "}
                    <Link to="/moderation">{t("dashboard.attentionRestrictions")}</Link>
                  </li>
                )}
                {data.attention.push_deliveries_retry_exhausted > 0 && (
                  <li>
                    <span className="kpi-value">
                      {data.attention.push_deliveries_retry_exhausted}
                    </span>{" "}
                    <Link to="/notifications" search={{ failed: "true" }}>
                      {t("dashboard.attentionPush")}
                    </Link>
                  </li>
                )}
              </ul>
            )}
          </section>

          <section>
            <h3>{t("dashboard.sectionActivity")}</h3>
            {data.recent_activity.length === 0 ? (
              <p role="status">{t("dashboard.activityEmpty")}</p>
            ) : (
              <ul className="activity-list">
                {data.recent_activity.map((entry) => (
                  <li key={entry.id}>
                    <span>
                      {actorOf(entry)} — {actionOf(entry)}
                    </span>
                    <time className="muted" dateTime={entry.created_at}>
                      {when(entry.created_at)}
                    </time>
                  </li>
                ))}
              </ul>
            )}
            <p>
              <Link to="/audit">{t("dashboard.viewAudit")}</Link>
            </p>
          </section>

          <section>
            <h3>{t("dashboard.sectionShortcuts")}</h3>
            <ul className="shortcut-list">
              <li>
                <Link to="/users">{t("dashboard.viewUsers")}</Link>
              </li>
              <li>
                <Link to="/matches">{t("dashboard.viewMatches")}</Link>
              </li>
              <li>
                <Link to="/tournaments">{t("dashboard.viewTournaments")}</Link>
              </li>
              <li>
                <Link to="/moderation">{t("dashboard.viewModeration")}</Link>
              </li>
              <li>
                <Link to="/notifications">{t("dashboard.viewNotifications")}</Link>
              </li>
            </ul>
          </section>
        </>
      )}
    </>
  );
}
