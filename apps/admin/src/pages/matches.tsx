import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { type AdminMatchSummary, fetchMatches, type MatchQuery } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";

/**
 * The Matches console — A64-024.4 §13.
 *
 * **Read-only, and it offers nothing it cannot do.** There is no
 * force-finish, no cancel, no result edit, and no disabled "coming soon"
 * control either: §3 is explicit that the UI must not promise a capability
 * that does not exist, and a greyed button is a promise.
 *
 * Filters are typed values in the URL, so a filtered view is a link an
 * operator can send. There is no free-text search box — the backend takes
 * enums and a participant **id**, because a name lives in another schema
 * and searching it here would mean a join DB-03 forbids. Finding the id is
 * the Users console's job, which is what the hint says.
 *
 * The pagination is the shape A64-024.3H settled: accumulate, dedupe by id,
 * reset on a filter change, and keep the loaded rows when a further page
 * fails. Not extracted into a shared hook — two usages is not the third
 * that earns an abstraction, and the two differ in their query type.
 */

const STATUSES = ["pending_acceptance", "active", "completed", "declined", "expired"] as const;
const ORIGINS = ["queue", "challenge", "rematch", "tournament"] as const;

type Search = { status?: string; rated?: string; origin?: string; participant?: string };

function tri(value: string | undefined): boolean | undefined {
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

export function MatchesPage() {
  const { t, locale } = useTranslation();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as Search;

  const [rows, setRows] = useState<AdminMatchSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreFailed, setMoreFailed] = useState(false);

  const query: MatchQuery = {
    ...(search.status ? { status: search.status } : {}),
    ...(tri(search.rated) !== undefined ? { rated: tri(search.rated) } : {}),
    ...(search.origin ? { origin: search.origin } : {}),
    ...(search.participant ? { participant_id: search.participant } : {}),
  };
  const key = JSON.stringify(query);

  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setState("loading");
    // A changed filter starts a new result set — an old cursor would ask
    // the server to continue a list that no longer exists.
    setRows([]);
    setCursor(null);
    setMoreFailed(false);

    void fetchMatches(query, next.signal).then((outcome) => {
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

    const outcome = await fetchMatches({ ...query, cursor });
    setLoadingMore(false);
    if (outcome.status !== "ok") {
      setMoreFailed(true);
      return;
    }
    setRows((current) => {
      const seen = new Set(current.map((row) => row.match_id));
      return [...current, ...outcome.value.items.filter((row) => !seen.has(row.match_id))];
    });
    setCursor(outcome.value.next_cursor);
  };

  const setFilter = (name: keyof Search, value: string) => {
    void navigate({
      to: "/matches",
      search: (current: Search) => ({ ...current, [name]: value || undefined }),
      replace: true,
    });
  };

  const seat = (player: AdminMatchSummary["light"]) =>
    player.display_name ?? player.username ?? player.player_id.slice(0, 8);

  const result = (match: AdminMatchSummary) =>
    match.winner
      ? `${t(match.winner === "light" ? "matches.light" : "matches.dark")}`
      : match.outcome
        ? match.outcome
        : t("matches.noResult");

  return (
    <>
      <h2>{t("matches.title")}</h2>

      <div className="filters">
        <p className="field">
          <label htmlFor="match-status">{t("matches.status")}</label>
          <select
            id="match-status"
            value={search.status ?? ""}
            onChange={(event) => setFilter("status", event.target.value)}
          >
            <option value="">{t("matches.any")}</option>
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </p>

        <p className="field">
          <label htmlFor="match-rated">{t("matches.ratedLabel")}</label>
          <select
            id="match-rated"
            value={search.rated ?? ""}
            onChange={(event) => setFilter("rated", event.target.value)}
          >
            <option value="">{t("matches.any")}</option>
            <option value="true">{t("matches.rated")}</option>
            <option value="false">{t("matches.casual")}</option>
          </select>
        </p>

        <p className="field">
          <label htmlFor="match-origin">{t("matches.origin")}</label>
          <select
            id="match-origin"
            value={search.origin ?? ""}
            onChange={(event) => setFilter("origin", event.target.value)}
          >
            <option value="">{t("matches.any")}</option>
            {ORIGINS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </p>

        <p className="field">
          <label htmlFor="match-participant">{t("matches.participant")}</label>
          <input
            id="match-participant"
            type="text"
            defaultValue={search.participant ?? ""}
            onBlur={(event) => setFilter("participant", event.target.value.trim())}
            aria-describedby="match-participant-hint"
          />
          <span id="match-participant-hint" className="muted">
            {t("matches.participantHint")}
          </span>
        </p>
      </div>

      {state === "loading" && <p role="status">{t("matches.loading")}</p>}
      {state === "error" && (
        <p role="alert" className="error">
          {t("matches.error")}
        </p>
      )}

      {state === "ready" && rows.length === 0 && (
        <>
          <p role="status">{t("matches.empty")}</p>
          <p className="muted">{t("matches.emptyHint")}</p>
        </>
      )}

      {state === "ready" && rows.length > 0 && (
        <>
          <table className="users-table">
            <thead>
              <tr>
                <th scope="col">{t("matches.colPlayers")}</th>
                <th scope="col">{t("matches.colStatus")}</th>
                <th scope="col">{t("matches.colResult")}</th>
                <th scope="col">{t("matches.colMode")}</th>
                <th scope="col">{t("matches.colOrigin")}</th>
                <th scope="col">{t("matches.colCreated")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((match) => (
                <tr key={match.match_id}>
                  <td>
                    <Link to="/matches/$matchId" params={{ matchId: match.match_id }}>
                      {seat(match.light)} — {seat(match.dark)}
                    </Link>
                  </td>
                  <td>{match.status}</td>
                  <td>{result(match)}</td>
                  <td>{t(match.rated ? "matches.rated" : "matches.casual")}</td>
                  <td>{match.origin}</td>
                  <td>{new Date(match.created_at).toLocaleDateString(locale)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Narrow: the same rows as cards, nothing dropped. */}
          <ul className="users-cards">
            {rows.map((match) => (
              <li key={match.match_id}>
                <Link to="/matches/$matchId" params={{ matchId: match.match_id }}>
                  {seat(match.light)} — {seat(match.dark)}
                </Link>
                <span>
                  {match.status} · {result(match)} ·{" "}
                  {t(match.rated ? "matches.rated" : "matches.casual")}
                </span>
                <span className="muted">
                  {match.origin} · {new Date(match.created_at).toLocaleDateString(locale)}
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
                {t(loadingMore ? "matches.loadingMore" : "matches.more")}
              </button>
            </p>
          )}

          {moreFailed && (
            <p role="alert" className="error">
              {t("matches.moreError")}
            </p>
          )}
        </>
      )}
    </>
  );
}
