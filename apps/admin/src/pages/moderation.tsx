import { Link } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { CATEGORY_LABELS } from "@/features/moderation/moderation-actions";
import {
  type AdminSanction,
  fetchRestrictions,
  type ModerationCategory,
} from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";

/**
 * The Moderation console — A64-024.6 §20.
 *
 * **Active restrictions, and the history behind them.** Not a case queue:
 * Arena64 has no player reports, and building an inbox for a stream that
 * does not exist would be a screen that is empty by construction.
 *
 * The actions are **not** here. Restricting an account happens on that
 * account's page, where an operator has just read who they are — a control
 * on a list row is one applied to whichever row was under the cursor.
 *
 * This page answers one operational question — who cannot sign in right
 * now — and links to the two places that answer the rest: the account, and
 * `/audit` for the full trail of who decided what.
 */
export function ModerationPage() {
  const { t, locale } = useTranslation();

  const [rows, setRows] = useState<AdminSanction[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [effectiveOnly, setEffectiveOnly] = useState(true);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreFailed, setMoreFailed] = useState(false);

  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setState("loading");
    setRows([]);
    setCursor(null);
    setMoreFailed(false);

    void fetchRestrictions({ effective_only: effectiveOnly }, next.signal).then((outcome) => {
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
  }, [effectiveOnly]);

  const loadMore = async () => {
    if (cursor === null || loadingMore) return;
    setLoadingMore(true);
    setMoreFailed(false);
    const outcome = await fetchRestrictions({ effective_only: effectiveOnly, cursor });
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

  const when = (value: string) => new Date(value).toLocaleString(locale);

  const reasonOf = (row: AdminSanction) => {
    const label = CATEGORY_LABELS[row.case.category as ModerationCategory];
    // An unknown category keeps its identifier. The trail outlives this
    // build, and a blank cell would hide a decision rather than show it.
    return label === undefined ? row.case.category : t(label);
  };

  const accountOf = (row: AdminSanction) => (
    <Link to="/users/$userId" params={{ userId: row.player_id }}>
      {row.username ?? row.player_id}
    </Link>
  );

  // Text, never colour alone — §27.
  const statusOf = (row: AdminSanction) =>
    t(row.is_effective ? "moderation.statusActive" : "moderation.statusEnded");

  const expiryOf = (row: AdminSanction) =>
    row.expires_at === null ? t("moderation.indefinite") : when(row.expires_at);

  return (
    <>
      <h2>{t("moderation.title")}</h2>
      <p className="muted">{t("moderation.lede")}</p>

      <div className="filters">
        <p className="field">
          <label htmlFor="moderation-scope">{t("moderation.colStatus")}</label>
          <select
            id="moderation-scope"
            value={effectiveOnly ? "effective" : "all"}
            onChange={(event) => setEffectiveOnly(event.target.value === "effective")}
          >
            <option value="effective">{t("moderation.showEffective")}</option>
            <option value="all">{t("moderation.showAll")}</option>
          </select>
        </p>
      </div>

      {state === "loading" && <p role="status">{t("moderation.loading")}</p>}
      {state === "error" && (
        <p role="alert" className="error">
          {t("moderation.error")}
        </p>
      )}
      {state === "ready" && rows.length === 0 && (
        <>
          <p role="status">{t("moderation.empty")}</p>
          <p className="muted">{t("moderation.emptyHint")}</p>
        </>
      )}

      {state === "ready" && rows.length > 0 && (
        <>
          <table className="users-table">
            <thead>
              <tr>
                <th scope="col">{t("moderation.colAccount")}</th>
                <th scope="col">{t("moderation.colReason")}</th>
                <th scope="col">{t("moderation.colSince")}</th>
                <th scope="col">{t("moderation.colExpires")}</th>
                <th scope="col">{t("moderation.colBy")}</th>
                <th scope="col">{t("moderation.colStatus")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{accountOf(row)}</td>
                  <td>{reasonOf(row)}</td>
                  <td>{when(row.starts_at)}</td>
                  <td>{expiryOf(row)}</td>
                  <td>{row.case.opened_by_username ?? row.case.opened_by}</td>
                  <td>{statusOf(row)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <ul className="users-cards">
            {rows.map((row) => (
              <li key={row.id}>
                <span>{accountOf(row)}</span>
                <span>
                  {reasonOf(row)} · {statusOf(row)}
                </span>
                <span className="muted">
                  {when(row.starts_at)} → {expiryOf(row)}
                </span>
                <span className="muted">
                  {t("moderation.colBy")}: {row.case.opened_by_username ?? row.case.opened_by}
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
                {t(loadingMore ? "moderation.loadingMore" : "moderation.more")}
              </button>
            </p>
          )}

          {moreFailed && (
            <p role="alert" className="error">
              {t("moderation.moreError")}
            </p>
          )}
        </>
      )}
    </>
  );
}
