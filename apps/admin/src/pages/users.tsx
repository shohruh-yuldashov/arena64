import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { type AdminUserSummary, fetchUsers, type UserQuery } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";

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
  /**
   * Every row loaded so far, oldest page first — A64-024.3H.
   *
   * Accumulated rather than replaced, because "Load more" is the UX: a
   * page that swapped its rows would lose the ones an operator had already
   * scrolled to. The server's ordering is deterministic and each cursor
   * continues where the last page ended, so appending preserves it.
   */
  const [rows, setRows] = useState<AdminUserSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  /**
   * Kept apart from `state`, deliberately.
   *
   * A failed *first* page has nothing to show and becomes the error
   * screen; a failed *next* page must leave the rows already on screen
   * exactly where they are. Folding the two into one state is how a
   * transient network blip erases an operator's place in a list.
   */
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreFailed, setMoreFailed] = useState(false);

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

  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setState("loading");
    // **The reset.** A changed search or filter starts a new result set,
    // so the accumulated rows and the cursor both go — reusing a cursor
    // from the previous query would ask the server to continue a list that
    // no longer exists, and it would answer with rows that do not match.
    setRows([]);
    setCursor(null);
    setMoreFailed(false);

    void fetchUsers(query, next.signal).then((outcome) => {
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
    // Keyed on the serialised query rather than on the object: a fresh
    // object each render would restart the request on every unrelated
    // state change, which is how a search box ends up cancelling itself.
  }, [key]);

  /**
   * Fetches the page after the one on screen and **appends** it.
   *
   * Deduplicated by id on the way in. The keyset is total — `(created_at,
   * id)` with a unique tiebreak — so a duplicate should be impossible; the
   * guard is here because the cost of being wrong is a React key collision
   * and a row rendered twice, and the cost of the guard is a `Set`.
   *
   * Uses its own request rather than the effect's controller, so a
   * superseded search still aborts the *first* page without this one
   * cancelling itself mid-append.
   */
  const loadMore = async () => {
    if (cursor === null || loadingMore) return;
    setLoadingMore(true);
    setMoreFailed(false);

    const outcome = await fetchUsers({ ...query, cursor });
    setLoadingMore(false);

    if (outcome.status !== "ok") {
      // The rows already on screen are untouched — §6.
      setMoreFailed(true);
      return;
    }

    setRows((current) => {
      const seen = new Set(current.map((row) => row.id));
      return [...current, ...outcome.value.items.filter((row) => !seen.has(row.id))];
    });
    setCursor(outcome.value.next_cursor);
  };

  const setFilter = (name: "active" | "verified", value: string) => {
    void navigate({
      to: "/users",
      search: (current: Search) => ({ ...current, [name]: value || undefined }),
      replace: true,
    });
  };

  return (
    <>
      <h2>{t("users.title")}</h2>

      <div className="filters">
        <p className="field">
          <label htmlFor="user-search">{t("users.search")}</label>
          <input
            id="user-search"
            type="search"
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            aria-describedby="user-search-hint"
          />
          <span id="user-search-hint" className="muted">
            {t("users.searchHint")}
          </span>
        </p>

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

      {state === "loading" && <p role="status">{t("users.loading")}</p>}

      {state === "error" && (
        <p role="alert" className="error">
          {t("users.error")}
        </p>
      )}

      {state === "ready" && rows.length === 0 && (
        <>
          <p role="status">{t("users.empty")}</p>
          <p className="muted">{t("users.emptyHint")}</p>
        </>
      )}

      {state === "ready" && rows.length > 0 && (
        <>
          {/* Wide: a table with real headers, so a screen reader can
              announce the column a cell belongs to. */}
          <table className="users-table">
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
              {rows.map((user) => (
                <tr key={user.id}>
                  <td>
                    <Link to="/users/$userId" params={{ userId: user.id }}>
                      {user.display_name ?? user.username}
                    </Link>
                  </td>
                  <td>{user.email}</td>
                  <td>{t(user.is_active ? "users.active" : "users.inactive")}</td>
                  <td>{t(user.is_verified ? "users.verified" : "users.unverified")}</td>
                  <td>{t(user.is_admin ? "users.roleAdmin" : "users.roleNone")}</td>
                  <td>{new Date(user.created_at).toLocaleDateString(locale)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Narrow: the same rows as cards. Nothing is dropped — every
              column above appears here as a labelled line. */}
          <ul className="users-cards">
            {rows.map((user) => (
              <li key={user.id}>
                <Link to="/users/$userId" params={{ userId: user.id }}>
                  {user.display_name ?? user.username}
                </Link>
                <span className="muted">{user.email}</span>
                <span>
                  {t(user.is_active ? "users.active" : "users.inactive")} ·{" "}
                  {t(user.is_verified ? "users.verified" : "users.unverified")}
                  {user.is_admin ? ` · ${t("users.roleAdmin")}` : ""}
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
          {cursor !== null && (
            <p className="load-more">
              <button
                type="button"
                className="action"
                disabled={loadingMore}
                onClick={() => void loadMore()}
              >
                {t(loadingMore ? "users.loadingMore" : "users.more")}
              </button>
            </p>
          )}

          {/* The rows above are still on screen — a failed next page must
              not cost an operator the ones they already had. */}
          {moreFailed && (
            <p role="alert" className="error">
              {t("users.moreError")}
            </p>
          )}
        </>
      )}
    </>
  );
}

export type { AdminUserSummary };
