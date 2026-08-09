import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { PUSH_SUMMARY_LABELS } from "@/features/notifications/vocabulary";
import {
  type AdminNotificationSummary,
  fetchNotifications,
  type NotificationQuery,
} from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";

/**
 * The Notification Operations console — A64-024.7 §17, §18.
 *
 * **There is no composer.** No send button, no recipient picker, no message
 * field, and no disabled "coming soon" control either: this platform has no
 * endpoint that creates a notification, and a screen implying otherwise
 * would be the first thing somebody asked for.
 *
 * ## The columns say what the platform actually knows
 *
 * The push column is never "Delivered". Web Push reports that a push
 * service accepted a request and nothing more, so the strongest truthful
 * phrase is "accepted by the push service" — see
 * `features/notifications/vocabulary`.
 *
 * The summary is the **worst** device's standing, which is why a
 * notification that reached two phones and failed on a laptop reads as
 * failed. A "mostly fine" badge would hide exactly the row an operator
 * opened this page to find.
 */

type Search = { recipient?: string; failed?: string };

export function NotificationsPage() {
  const { t, locale } = useTranslation();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as Search;

  const [rows, setRows] = useState<AdminNotificationSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreFailed, setMoreFailed] = useState(false);

  const query: NotificationQuery = {
    ...(search.recipient ? { recipient_id: search.recipient } : {}),
    ...(search.failed === "true" ? { failed_push_only: true } : {}),
  };
  const key = JSON.stringify(query);

  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    // A filter change resets the accumulation as well as the cursor —
    // appending a differently-filtered page to the old rows would show a
    // list that answers two questions at once.
    setState("loading");
    setRows([]);
    setCursor(null);
    setMoreFailed(false);

    void fetchNotifications(query, next.signal).then((outcome) => {
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
    const outcome = await fetchNotifications({ ...query, cursor });
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
      to: "/notifications",
      search: (current: Search) => ({ ...current, [name]: value || undefined }),
      replace: true,
    });
  };

  const when = (value: string) => new Date(value).toLocaleString(locale);

  const pushOf = (row: AdminNotificationSummary) => {
    const label = PUSH_SUMMARY_LABELS[row.push_summary];
    // An unknown standing keeps its identifier: the backend outlives this
    // build, and a blank cell would read as "nothing happened".
    return label === undefined ? row.push_summary : t(label);
  };

  const recipientOf = (row: AdminNotificationSummary) => (
    <Link to="/users/$userId" params={{ userId: row.recipient_id }}>
      {row.recipient_username ?? row.recipient_id}
    </Link>
  );

  return (
    <>
      <h2>{t("notifications.title")}</h2>
      <p className="muted">{t("notifications.lede")}</p>

      <div className="filters">
        <p className="field">
          <label htmlFor="notification-recipient">{t("notifications.filterRecipient")}</label>
          <input
            id="notification-recipient"
            type="search"
            value={search.recipient ?? ""}
            placeholder={t("notifications.recipientPlaceholder")}
            onChange={(event) => setFilter("recipient", event.target.value.trim())}
          />
        </p>

        <p className="field">
          <label htmlFor="notification-scope">{t("notifications.filterScope")}</label>
          <select
            id="notification-scope"
            value={search.failed === "true" ? "failed" : "all"}
            onChange={(event) =>
              setFilter("failed", event.target.value === "failed" ? "true" : "")
            }
          >
            <option value="all">{t("notifications.scopeAll")}</option>
            <option value="failed">{t("notifications.scopeFailed")}</option>
          </select>
        </p>
      </div>

      {state === "loading" && <p role="status">{t("notifications.loading")}</p>}
      {state === "error" && (
        <p role="alert" className="error">
          {t("notifications.error")}
        </p>
      )}
      {state === "ready" && rows.length === 0 && (
        <>
          <p role="status">{t("notifications.empty")}</p>
          <p className="muted">{t("notifications.emptyHint")}</p>
        </>
      )}

      {state === "ready" && rows.length > 0 && (
        <>
          <table className="users-table">
            <thead>
              <tr>
                <th scope="col">{t("notifications.colWhen")}</th>
                <th scope="col">{t("notifications.colRecipient")}</th>
                <th scope="col">{t("notifications.colType")}</th>
                <th scope="col">{t("notifications.colInApp")}</th>
                <th scope="col">{t("notifications.colPush")}</th>
                <th scope="col">{t("notifications.colDevices")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <Link
                      to="/notifications/$notificationId"
                      params={{ notificationId: row.id }}
                    >
                      {when(row.created_at)}
                    </Link>
                  </td>
                  <td>{recipientOf(row)}</td>
                  <td>{row.type}</td>
                  {/* Text, never colour alone — §24. */}
                  <td>
                    {t(row.read_at === null ? "notifications.unread" : "notifications.read")}
                  </td>
                  <td>{pushOf(row)}</td>
                  <td>{row.delivery_count}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <ul className="users-cards">
            {rows.map((row) => (
              <li key={row.id}>
                <Link to="/notifications/$notificationId" params={{ notificationId: row.id }}>
                  {row.type}
                </Link>
                <span>{recipientOf(row)}</span>
                <span className="muted">
                  {when(row.created_at)} ·{" "}
                  {t(row.read_at === null ? "notifications.unread" : "notifications.read")}
                </span>
                <span className="muted">
                  {t("notifications.colPush")}: {pushOf(row)} · {row.delivery_count}
                </span>
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
                {t(loadingMore ? "notifications.loadingMore" : "notifications.more")}
              </button>
            </p>
          )}

          {moreFailed && (
            <p role="alert" className="error">
              {t("notifications.moreError")}
            </p>
          )}
        </>
      )}
    </>
  );
}
