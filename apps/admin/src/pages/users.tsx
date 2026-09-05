import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";

import { type AdminUserSummary, fetchUsers, type UserQuery } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { DataTable } from "@/shared/ui/data-table";
import { Icon } from "@/shared/ui/icon";
import { StatusBadge } from "@/shared/ui/status-badge";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/shared/ui/states";
import { PageHeader } from "@/shared/ui/page-header";
import { Pagination } from "@/shared/ui/pagination";
import { useCursorPages } from "@/shared/ui/use-cursor-pages";

/**
 * The Users console — A64-024.3 §11, §12, §14.
 *
 * **Read-only.** There is no action on this page and none on the detail
 * page, because `admin.audit_entry` is not built and §8 forbids an
 * unaudited administrative mutation. A "Deactivate" button would be the
 * easiest thing here to add and the hardest to justify.
 *
 * ## Search state lives in the URL
 *
 * The router already owns it, so a filtered search is a link an operator
 * can send to a colleague, and the back button works. Debounced rather than
 * submit-based so typing feels live, and every superseded request is
 * **aborted** — without that a slow first response can land after a fast
 * second and repaint stale rows.
 *
 * ## Responsive — §12, §18
 *
 * One table on a wide screen. Below the breakpoint the same rows render as
 * cards, because a six-column table at 360px is either a horizontal scroll
 * nobody finds or a font nobody reads. Both render the same data from the
 * same array; nothing is hidden at either width.
 */

const DEBOUNCE_MS = 300;

type Search = { q?: string; active?: string; verified?: string };

/** `"any"` is absence, so the URL stays clean when nothing is narrowed. */
function tri(value: string | undefined): boolean | undefined {
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

export function UsersPage() {
  const { t, locale } = useTranslation();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as Search;

  const [term, setTerm] = useState(search.q ?? "");
  // Keeps the input responsive while the URL — the thing that actually
  // drives the query — updates only once typing settles.
  useEffect(() => {
    const timer = setTimeout(() => {
      if ((search.q ?? "") === term) return;
      void navigate({
        to: "/users",
        search: (current: Search) => ({ ...current, q: term || undefined }),
        replace: true,
      });
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [term, search.q, navigate]);

  const query: UserQuery = {
    ...(search.q ? { q: search.q } : {}),
    ...(tri(search.active) !== undefined ? { is_active: tri(search.active) } : {}),
    ...(tri(search.verified) !== undefined ? { is_verified: tri(search.verified) } : {}),
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
  const pages = useCursorPages<AdminUserSummary>(
    useCallback(
      (cursor, signal) => fetchUsers({ ...query, ...(cursor ? { cursor } : {}) }, signal),
      [key],
    ),
    key,
  );

  const setFilter = (name: "active" | "verified", value: string) => {
    void navigate({
      to: "/users",
      search: (current: Search) => ({ ...current, [name]: value || undefined }),
      replace: true,
    });
  };

  return (
    <>
      <PageHeader title={t("users.title")} />

      <div className="toolbar">
        <span className="search">
          <Icon name="search" size={16} />
          <label htmlFor="user-search" className="sr-only">
            {t("users.search")}
          </label>
          <input
            id="user-search"
            type="search"
            value={term}
            placeholder={t("users.search")}
            onChange={(event) => setTerm(event.target.value)}
            aria-describedby="user-search-hint"
          />
        </span>
        <span id="user-search-hint" className="muted">
          {t("users.searchHint")}
        </span>
      </div>

      <div className="filters">
        <p className="field">
          <label htmlFor="filter-active">{t("users.colStatus")}</label>
          <select
            id="filter-active"
            value={search.active ?? ""}
            onChange={(event) => setFilter("active", event.target.value)}
          >
            <option value="">{t("users.any")}</option>
            <option value="true">{t("users.active")}</option>
            <option value="false">{t("users.inactive")}</option>
          </select>
        </p>

        <p className="field">
          <label htmlFor="filter-verified">{t("users.colVerified")}</label>
          <select
            id="filter-verified"
            value={search.verified ?? ""}
            onChange={(event) => setFilter("verified", event.target.value)}
          >
            <option value="">{t("users.any")}</option>
            <option value="true">{t("users.verified")}</option>
            <option value="false">{t("users.unverified")}</option>
          </select>
        </p>
      </div>

      {pages.state === "loading" && <LoadingSkeleton label={t("users.loading")} />}

      {pages.state === "error" && (
        <ErrorState title={t("users.error")} onRetry={pages.reload} />
      )}

      {pages.state === "ready" && pages.rows.length === 0 && (
        <EmptyState icon="users" title={t("users.empty")} description={t("users.emptyHint")} />
      )}

      {pages.state === "ready" && pages.rows.length > 0 && (
        <>
          {/* Wide: a table with real headers, so a screen reader can
              announce the column a cell belongs to. */}
          <DataTable caption={t("users.title")} minWidth="52rem">
            <thead>
              <tr>
                <th scope="col">{t("users.colUser")}</th>
                <th scope="col">{t("users.colEmail")}</th>
                <th scope="col">{t("users.colStatus")}</th>
                <th scope="col">{t("users.colVerified")}</th>
                <th scope="col">{t("users.colRole")}</th>
                <th scope="col">{t("users.colJoined")}</th>
              </tr>
            </thead>
            <tbody>
              {pages.rows.map((user) => (
                <tr key={user.id}>
                  <th scope="row">
                    <Link
                      className="cell-primary"
                      to="/users/$userId"
                      params={{ userId: user.id }}
                    >
                      <strong>{user.display_name ?? user.username}</strong>
                      <span>@{user.username}</span>
                    </Link>
                  </th>
                  <td>{user.email}</td>
                  {/* A word and a hue, not a hue: an operator scanning a
                      hundred rows uses the colour, everybody else uses the
                      word, and forced-colours mode keeps the dot. */}
                  <td>
                    <StatusBadge
                      label={t(user.is_active ? "users.active" : "users.inactive")}
                      tone={user.is_active ? "success" : "neutral"}
                    />
                  </td>
                  <td>
                    <StatusBadge
                      label={t(user.is_verified ? "users.verified" : "users.unverified")}
                      tone={user.is_verified ? "success" : "warning"}
                    />
                  </td>
                  <td>
                    {user.is_admin ? (
                      <StatusBadge label={t("users.roleAdmin")} tone="primary" />
                    ) : (
                      <span className="muted">{t("users.roleNone")}</span>
                    )}
                  </td>
                  <td>{new Date(user.created_at).toLocaleDateString(locale)}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>

          {/* Narrow: the same rows as cards. Nothing is dropped — every
              column above appears here as a labelled line. */}
          <ul className="users-cards">
            {pages.rows.map((user) => (
              <li key={user.id}>
                <Link to="/users/$userId" params={{ userId: user.id }}>
                  {user.display_name ?? user.username}
                </Link>
                <span className="muted">{user.email}</span>
                <span className="badges">
                  <StatusBadge
                    label={t(user.is_active ? "users.active" : "users.inactive")}
                    tone={user.is_active ? "success" : "neutral"}
                  />
                  <StatusBadge
                    label={t(user.is_verified ? "users.verified" : "users.unverified")}
                    tone={user.is_verified ? "success" : "warning"}
                  />
                  {user.is_admin && <StatusBadge label={t("users.roleAdmin")} tone="primary" />}
                </span>
                <span className="muted">
                  {t("users.joined")}: {new Date(user.created_at).toLocaleDateString(locale)}
                </span>
              </li>
            ))}
          </ul>

          {/* §6: gone entirely when the server sent no cursor. A disabled
              "Load more" on the last page is a control that says there is
              something else and then refuses to fetch it. */}
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

export type { AdminUserSummary };
