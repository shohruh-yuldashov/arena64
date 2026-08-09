import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback } from "react";

import { PUSH_SUMMARY_LABELS } from "@/features/notifications/vocabulary";
import {
  type AdminNotificationSummary,
  fetchNotifications,
  type NotificationQuery,
} from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { Pagination } from "@/shared/ui/pagination";
import { useCursorPages } from "@/shared/ui/use-cursor-pages";

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

  const query: NotificationQuery = {
    ...(search.recipient ? { recipient_id: search.recipient } : {}),
    ...(search.failed === "true" ? { failed_push_only: true } : {}),
  };
  const key = JSON.stringify(query);

  /**
   * One page at a time, walked by cursor — A64-024 hardening.
   *
   * Replaces the accumulating "Load more": an operator nine pages into a
   * listing had eight pages of rows above the one they were reading and no
   * way back. The hook holds the cursor that produced each page, so
   * `Previous` is a re-fetch with a cursor already in hand and the keyset
   * the server offers is unchanged.
   */
  const pages = useCursorPages<AdminNotificationSummary>(
    useCallback(
      (cursor, signal) =>
        fetchNotifications({ ...query, ...(cursor ? { cursor } : {}) }, signal),
      [key],
    ),
    key,
  );

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

      {pages.state === "loading" && <p role="status">{t("notifications.loading")}</p>}
      {pages.state === "error" && (
        <p role="alert" className="error">
          {t("notifications.error")}
        </p>
      )}
      {pages.state === "ready" && pages.rows.length === 0 && (
        <>
          <p role="status">{t("notifications.empty")}</p>
          <p className="muted">{t("notifications.emptyHint")}</p>
        </>
      )}

      {pages.state === "ready" && pages.rows.length > 0 && (
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
              {pages.rows.map((row) => (
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
            {pages.rows.map((row) => (
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
