import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { type AdminAuditEntry, type AuditQuery, fetchAuditEntries } from "@/shared/api/client";
import { type TranslationKey, useTranslation } from "@/shared/i18n";

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

/** The actions this build can phrase. Anything else falls back to its id. */
const ACTION_LABELS: Record<string, TranslationKey> = {
  "admin.role.grant": "audit.actionRoleGrant",
  "admin.role.revoke": "audit.actionRoleRevoke",
};

/**
 * Subject types this console can link to — a **closed** map on purpose.
 *
 * An unknown subject type renders as plain text. Building a link from an
 * unrecognised type would produce a route that does not exist, and a
 * broken link in an incident review is worse than no link at all.
 */
const SUBJECT_ROUTES: Record<string, string> = {
  account: "/users",
  match: "/matches",
  tournament: "/tournaments",
};

type Search = { action?: string; actor?: string; subject?: string };

export function AuditPage() {
  const { t, locale } = useTranslation();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as Search;

  const [rows, setRows] = useState<AdminAuditEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreFailed, setMoreFailed] = useState(false);

  const query: AuditQuery = {
    ...(search.action ? { action: search.action } : {}),
    ...(search.actor ? { actor_id: search.actor } : {}),
    // The pair travels together or not at all — the server refuses a bare
    // ref, because the index it would need leads with the type.
    ...(search.subject ? { subject_type: "account", subject_ref: search.subject } : {}),
  };
  const key = JSON.stringify(query);

  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setState("loading");
    setRows([]);
    setCursor(null);
    setMoreFailed(false);

    void fetchAuditEntries(query, next.signal).then((outcome) => {
      if (next.signal.aborted) return;
      if (outcome.status === "ok") {
        setRows(outcome.value.items);
        setCursor(outcome.value.next_cursor);
        setState("ready");
        return;
      }
      setState("error");
    });

    return () => next.abort();
  }, [key]);

  const loadMore = async () => {
    if (cursor === null || loadingMore) return;
    setLoadingMore(true);
    setMoreFailed(false);
    const outcome = await fetchAuditEntries({ ...query, cursor });
    setLoadingMore(false);
    if (outcome.status !== "ok") {
      setMoreFailed(true);
      return;
    }
    setRows((current) => {
      const seen = new Set(current.map((row) => row.id));
      return [...current, ...outcome.value.items.filter((row) => !seen.has(row.id))];
    });
    setCursor(outcome.value.next_cursor);
  };

  const setFilter = (name: keyof Search, value: string) => {
    void navigate({
      to: "/audit",
      search: (current: Search) => ({ ...current, [name]: value || undefined }),
      replace: true,
    });
  };

  const when = (value: string) => new Date(value).toLocaleString(locale);

  const actionOf = (entry: AdminAuditEntry) => {
    const label = ACTION_LABELS[entry.action];
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
    const route = SUBJECT_ROUTES[entry.subject.type];
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
      <h2>{t("audit.title")}</h2>
      <p className="muted">{t("audit.lede")}</p>

      <div className="filters">
        <p className="field">
          <label htmlFor="audit-action">{t("audit.filterAction")}</label>
          <select
            id="audit-action"
            value={search.action ?? ""}
            onChange={(event) => setFilter("action", event.target.value)}
          >
            <option value="">{t("audit.any")}</option>
            {Object.keys(ACTION_LABELS).map((value) => (
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

      {state === "loading" && <p role="status">{t("audit.loading")}</p>}
      {state === "error" && (
        <p role="alert" className="error">
          {t("audit.error")}
        </p>
      )}
      {state === "ready" && rows.length === 0 && (
        <>
          <p role="status">{t("audit.empty")}</p>
          <p className="muted">{t("audit.emptyHint")}</p>
        </>
      )}

      {state === "ready" && rows.length > 0 && (
        <>
          <table className="users-table">
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
              {rows.map((entry) => (
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
          </table>

          <ul className="users-cards">
            {rows.map((entry) => (
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

          {cursor !== null && (
            <p className="load-more">
              <button
                type="button"
                className="action"
                disabled={loadingMore}
                onClick={() => void loadMore()}
              >
                {t(loadingMore ? "audit.loadingMore" : "audit.more")}
              </button>
            </p>
          )}

          {moreFailed && (
            <p role="alert" className="error">
              {t("audit.moreError")}
            </p>
          )}
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
