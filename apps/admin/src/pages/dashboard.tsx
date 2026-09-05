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
 * ## What needs a person comes first — A64-027A §7
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
            <button
              type="button"
              className="action"
              disabled={refreshing}
              onClick={() => void load(true)}
            >
              <Icon name="refresh" size={16} />
              {t(refreshing ? "dashboard.refreshing" : "dashboard.refresh")}
            </button>
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
          <p className="dashboard-freshness">
            <span className="muted">
              {t("dashboard.updatedAt", { at: when(data.generated_at) })}
            </span>
          </p>

          {refreshFailed && <ErrorNotice message={t("dashboard.error")} />}

          <Section title={t("dashboard.sectionAttention")}>
            {attentionTotal === 0 ? (
              <p className="notice" data-tone="success" role="status">
                <Icon name="success" size={17} />
                <span>
                  <strong>{t("dashboard.attentionClear")}</strong>{" "}
                  <span className="muted">{t("dashboard.attentionClearHint")}</span>
                </span>
              </p>
            ) : (
              <ul className="kpi-grid">
                {data.attention.restrictions_in_force > 0 && (
                  <li>
                    <StatCard
                      label={t("dashboard.attentionRestrictions")}
                      value={data.attention.restrictions_in_force}
                      icon="moderation"
                      tone="warning"
                      foot={<Link to="/moderation">{t("dashboard.viewModeration")}</Link>}
                    />
                  </li>
                )}
                {data.attention.push_deliveries_retry_exhausted > 0 && (
                  <li>
                    <StatCard
                      label={t("dashboard.attentionPush")}
                      value={data.attention.push_deliveries_retry_exhausted}
                      icon="notifications"
                      tone="danger"
                      foot={
                        <Link to="/notifications" search={{ failed: "true" }}>
                          {t("dashboard.viewNotifications")}
                        </Link>
                      }
                    />
                  </li>
                )}
              </ul>
            )}
          </Section>

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

          <Section
            title={t("dashboard.sectionActivity")}
            aside={<Link to="/audit">{t("dashboard.viewAudit")}</Link>}
          >
            {data.recent_activity.length === 0 ? (
              <p className="muted" role="status">
                {t("dashboard.activityEmpty")}
              </p>
            ) : (
              <ul className="timeline">
                {data.recent_activity.map((entry) => (
                  <li
                    key={entry.id}
                    data-tone={entry.outcome === "succeeded" ? "primary" : undefined}
                  >
                    <div className="cell-primary">
                      <strong>{actionOf(entry)}</strong>
                      <span>
                        {actorOf(entry)}
                        {" · "}
                        <time dateTime={entry.created_at}>{when(entry.created_at)}</time>
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </>
      )}
    </>
  );
}
