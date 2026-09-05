import { Link } from "@tanstack/react-router";
import { useCallback, useState } from "react";

import { CATEGORY_LABELS } from "@/features/moderation/moderation-actions";
import {
  type AdminSanction,
  fetchRestrictions,
  type ModerationCategory,
} from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { DataTable } from "@/shared/ui/data-table";
import { FilterToolbar, SelectField } from "@/shared/ui/filter-toolbar";
import { Icon } from "@/shared/ui/icon";
import { StatusBadge } from "@/shared/ui/status-badge";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/shared/ui/states";
import { PageHeader } from "@/shared/ui/page-header";
import { Pagination } from "@/shared/ui/pagination";
import { useCursorPages } from "@/shared/ui/use-cursor-pages";

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

  const [effectiveOnly, setEffectiveOnly] = useState(true);

  /**
   * One page at a time, walked by cursor — A64-024 hardening.
   *
   * The scope toggle is the query here, so it is the key: switching it
   * discards the cursor history and restarts at page one, because a cursor
   * from the effective-only walk names a row the full history may order
   * differently.
   */
  const pages = useCursorPages<AdminSanction>(
    useCallback(
      (cursor, signal) =>
        fetchRestrictions(
          { effective_only: effectiveOnly, ...(cursor ? { cursor } : {}) },
          signal,
        ),
      [effectiveOnly],
    ),
    String(effectiveOnly),
  );

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
  const statusOf = (row: AdminSanction) => (
    <StatusBadge
      label={t(row.is_effective ? "moderation.statusActive" : "moderation.statusEnded")}
      tone={row.is_effective ? "danger" : "neutral"}
    />
  );

  /** Indefinite is a *state*, not a missing date — §22. */
  const expiry = (row: AdminSanction) =>
    row.expires_at === null ? (
      <StatusBadge label={t("moderation.indefinite")} tone="warning" />
    ) : (
      <span>{when(row.expires_at)}</span>
    );

  const expiryOf = (row: AdminSanction) =>
    row.expires_at === null ? t("moderation.indefinite") : when(row.expires_at);

  return (
    <>
      <PageHeader title={t("moderation.title")} description={t("moderation.lede")} />

      <FilterToolbar
        filters={
          <SelectField
            label={t("moderation.scope")}
            value={effectiveOnly ? "effective" : "all"}
            onChange={(value) => {
              setEffectiveOnly(value === "effective");
            }}
          >
            <option value="effective">{t("moderation.scopeEffective")}</option>
            <option value="all">{t("moderation.scopeAll")}</option>
          </SelectField>
        }
      />

      {pages.state === "loading" && <LoadingSkeleton label={t("moderation.loading")} />}
      {pages.state === "error" && (
        <ErrorState title={t("moderation.error")} onRetry={pages.reload} />
      )}
      {pages.state === "ready" && pages.rows.length === 0 && (
        <EmptyState
          icon="moderation"
          title={t("moderation.empty")}
          description={t("moderation.emptyHint")}
        />
      )}

      {pages.state === "ready" && pages.rows.length > 0 && (
        <>
          <DataTable caption={t("moderation.title")} minWidth="46rem">
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
              {pages.rows.map((row) => (
                <tr key={row.id}>
                  <th scope="row">{accountOf(row)}</th>
                  <td>
                    <span className="reason">
                      <Icon name="moderation" size={15} />
                      {reasonOf(row)}
                    </span>
                  </td>
                  <td>{when(row.starts_at)}</td>
                  <td>{expiry(row)}</td>
                  <td>{row.case.opened_by_username ?? row.case.opened_by}</td>
                  <td>{statusOf(row)}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>

          <ul className="users-cards">
            {pages.rows.map((row) => (
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
