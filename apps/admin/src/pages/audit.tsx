import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback } from "react";

import { AUDIT_ACTION_LABELS, AUDIT_SUBJECT_ROUTES } from "@/features/audit/vocabulary";
import { type AdminAuditEntry, type AuditQuery, fetchAuditEntries } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { DataTable } from "@/shared/ui/data-table";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/shared/ui/states";
import { PageHeader } from "@/shared/ui/page-header";
import { Pagination } from "@/shared/ui/pagination";
import { useCursorPages } from "@/shared/ui/use-cursor-pages";

/**
 * The Audit console — A64-024.8.
 *
 * **Read-only, permanently.** Not "read-only for now" like the sections
 * before it: an entry is written by the service performing the action, and
 * there is no endpoint that accepts one. A console that could append to the
 * audit trail would be a console that could write history of things that
 * never happened.
 *
 * ## The server sends facts; this composes the sentence
 *
 * The API returns `action`, `actor` and `subject`. "Sanjar granted the
 * admin role to Aziza" is assembled here, in the operator's own language —
 * a server returning that string would put the platform's languages in the
 * API and require a deployment to add one.
 *
 * An action this build does not know renders as its raw identifier rather
 * than as a blank cell. The trail is older than the console reading it, and
 * an entry nobody can read is worse than an ugly one.
 */

type Search = { action?: string; actor?: string; subject?: string };

export function AuditPage() {
  const { t, locale } = useTranslation();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as Search;

  const query: AuditQuery = {
    ...(search.action ? { action: search.action } : {}),
    ...(search.actor ? { actor_id: search.actor } : {}),
    // The pair travels together or not at all — the server refuses a bare
    // ref, because the index it would need leads with the type.
    ...(search.subject ? { subject_type: "account", subject_ref: search.subject } : {}),
  };
  const key = JSON.stringify(query);

  /**
   * One page at a time, walked by cursor — A64-024 hardening.
   *
   * Replaces the accumulating "Load more": an operator nine pages
   * into a listing had eight pages of rows above the one they were
   * reading and no way back. The hook holds the cursor that produced
   * each page, so `Previous` is a re-fetch with a cursor already in
   * hand and the keyset the server offers is unchanged.
   */
  const pages = useCursorPages<AdminAuditEntry>(
    useCallback(
      (cursor, signal) =>
        fetchAuditEntries({ ...query, ...(cursor ? { cursor } : {}) }, signal),
      [key],
    ),
    key,
  );

  const setFilter = (name: keyof Search, value: string) => {
    void navigate({
      to: "/audit",
      search: (current: Search) => ({ ...current, [name]: value || undefined }),
      replace: true,
    });
  };

  const when = (value: string) => new Date(value).toLocaleString(locale);

  const actionOf = (entry: AdminAuditEntry) => {
    const label = AUDIT_ACTION_LABELS[entry.action];
    return label === undefined ? entry.action : t(label);
  };

  const actorOf = (entry: AdminAuditEntry) => {
    // No account means an operator process, which is a fact rather than a
    // gap: the deployment's first grant is made from a shell before any
    // administrator exists.
    if (entry.actor.account_id === null)
      return <span className="muted">{t("audit.operator")}</span>;
    return (
      <Link to="/users/$userId" params={{ userId: entry.actor.account_id }}>
        {entry.actor.username ?? entry.actor.account_id}
      </Link>
    );
  };

  const subjectOf = (entry: AdminAuditEntry) => {
    const route = AUDIT_SUBJECT_ROUTES[entry.subject.type];
    const label = entry.subject.username ?? entry.subject.ref;
    if (route === undefined) {
      return (
        <span>
          {entry.subject.type} · {entry.subject.ref}
        </span>
      );
    }
    if (route === "/users") {
      return (
        <Link to="/users/$userId" params={{ userId: entry.subject.ref }}>
          {label}
        </Link>
      );
    }
    if (route === "/matches") {
      return (
        <Link to="/matches/$matchId" params={{ matchId: entry.subject.ref }}>
          {label}
        </Link>
      );
    }
    if (route === "/notifications") {
      return (
        <Link
          to="/notifications/$notificationId"
          params={{ notificationId: entry.subject.ref }}
        >
          {label}
        </Link>
      );
    }
    return (
      <Link to="/tournaments/$tournamentId" params={{ tournamentId: entry.subject.ref }}>
        {label}
      </Link>
    );
  };

  const outcomeOf = (entry: AdminAuditEntry) =>
    entry.outcome === "succeeded" ? t("audit.outcomeSucceeded") : t("audit.outcomeFailed");

  const details = (entry: AdminAuditEntry) => (
    <details>
      <summary>{t("audit.details")}</summary>
      <dl className="audit-details">
        <dt>{t("audit.before")}</dt>
        <dd>{describe(entry.before) || t("audit.none")}</dd>
        <dt>{t("audit.after")}</dt>
        <dd>{describe(entry.after) || t("audit.none")}</dd>
        <dt>{t("audit.correlation")}</dt>
        <dd>{entry.correlation_id ?? t("audit.none")}</dd>
      </dl>
    </details>
  );

  return (
    <>
      <PageHeader title={t("audit.title")} description={t("audit.lede")} />

      <div className="filters">
        <p className="field">
          <label htmlFor="audit-action">{t("audit.filterAction")}</label>
          <select
            id="audit-action"
            value={search.action ?? ""}
            onChange={(event) => setFilter("action", event.target.value)}
          >
            <option value="">{t("audit.any")}</option>
            {Object.keys(AUDIT_ACTION_LABELS).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </p>

        <p className="field">
          <label htmlFor="audit-subject">{t("audit.filterSubject")}</label>
          <input
            id="audit-subject"
            type="search"
            value={search.subject ?? ""}
            placeholder={t("audit.subjectPlaceholder")}
            onChange={(event) => setFilter("subject", event.target.value.trim())}
          />
        </p>
      </div>

      {pages.state === "loading" && <LoadingSkeleton label={t("audit.loading")} />}
      {pages.state === "error" && (
        <ErrorState title={t("audit.error")} onRetry={pages.reload} />
      )}
      {pages.state === "ready" && pages.rows.length === 0 && (
        <EmptyState icon="audit" title={t("audit.empty")} description={t("audit.emptyHint")} />
      )}

      {pages.state === "ready" && pages.rows.length > 0 && (
        <>
          <DataTable caption={t("audit.title")} minWidth="54rem">
            <thead>
              <tr>
                <th scope="col">{t("audit.colWhen")}</th>
                <th scope="col">{t("audit.colActor")}</th>
                <th scope="col">{t("audit.colAction")}</th>
                <th scope="col">{t("audit.colSubject")}</th>
                <th scope="col">{t("audit.colOutcome")}</th>
              </tr>
            </thead>
            <tbody>
              {pages.rows.map((entry) => (
                <tr key={entry.id}>
                  <td>{when(entry.created_at)}</td>
                  <td>{actorOf(entry)}</td>
                  <td>
                    {actionOf(entry)}
                    {details(entry)}
                  </td>
                  <td>{subjectOf(entry)}</td>
                  <td>{outcomeOf(entry)}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>

          <ul className="users-cards">
            {pages.rows.map((entry) => (
              <li key={entry.id}>
                <span>
                  {actorOf(entry)} {actionOf(entry)}
                </span>
                <span>{subjectOf(entry)}</span>
                <span className="muted">
                  {when(entry.created_at)} · {outcomeOf(entry)}
                </span>
                {details(entry)}
              </li>
            ))}
          </ul>

          <Pagination
            page={pages.page}
            hasPrevious={pages.hasPrevious}
            hasNext={pages.hasNext}
            busy={pages.busy}
            onPrevious={pages.previous}
            onNext={pages.next}
          />
        </>
      )}
    </>
  );
}

/**
 * A `before`/`after` slice as one readable line.
 *
 * `key: value` pairs rather than raw JSON, because the slices are small and
 * written by a use case — and because an operator reading an incident
 * should not have to parse braces. Empty renders as nothing, which the
 * caller turns into a dash.
 */
function describe(slice: Record<string, unknown>): string {
  return Object.entries(slice)
    .map(([field, value]) => `${field}: ${String(value)}`)
    .join(" · ");
}
