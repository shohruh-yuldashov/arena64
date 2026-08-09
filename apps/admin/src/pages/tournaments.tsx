import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AdminTournamentSummary,
  fetchTournaments,
  type TournamentQuery,
} from "@/shared/api/client";
import { CreateTournament } from "@/features/tournaments/create-tournament";
import { useTranslation } from "@/shared/i18n";

/**
 * The Tournaments console — A64-024.5 §16, A64-024.5H.
 *
 * It offers nothing it cannot do. Creation arrived in A64-024.5H because
 * `tournament` has a canonical use case for it; cancel and publish-round
 * did not, and there is no disabled "coming soon" control for either — a
 * greyed button implies the platform has an answer it is withholding.
 *
 * Filters are typed values in the URL. There is **no name search box** —
 * `tournament.name` carries no index, so a substring match would be a
 * sequential scan; the backend does not offer it and the console does not
 * pretend to.
 */

const STATUSES = [
  "draft",
  "registration_open",
  "registration_closed",
  "in_progress",
  "completed",
  "cancelled",
] as const;

type Search = { status?: string; rated?: string };

function tri(value: string | undefined): boolean | undefined {
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

export function TournamentsPage() {
  const { t, locale } = useTranslation();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as Search;

  const [rows, setRows] = useState<AdminTournamentSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreFailed, setMoreFailed] = useState(false);

  const query: TournamentQuery = {
    ...(search.status ? { status: search.status } : {}),
    ...(tri(search.rated) !== undefined ? { rated: tri(search.rated) } : {}),
  };
  const key = JSON.stringify(query);

  const controller = useRef<AbortController | null>(null);
  const reload = useCallback(() => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setState("loading");
    setRows([]);
    setCursor(null);
    setMoreFailed(false);

    return fetchTournaments(JSON.parse(key) as TournamentQuery, next.signal).then((outcome) => {
      if (next.signal.aborted) return;
      if (outcome.status === "ok") {
        setRows(outcome.value.items);
        setCursor(outcome.value.next_cursor);
        setState("ready");
        return;
      }
      setState("error");
    });
  }, [key]);

  useEffect(() => {
    void reload();
    return () => controller.current?.abort();
  }, [reload]);

  const loadMore = async () => {
    if (cursor === null || loadingMore) return;
    setLoadingMore(true);
    setMoreFailed(false);
    const outcome = await fetchTournaments({ ...query, cursor });
    setLoadingMore(false);
    if (outcome.status !== "ok") {
      setMoreFailed(true);
      return;
    }
    setRows((current) => {
      const seen = new Set(current.map((row) => row.tournament_id));
      return [...current, ...outcome.value.items.filter((row) => !seen.has(row.tournament_id))];
    });
    setCursor(outcome.value.next_cursor);
  };

  const setFilter = (name: keyof Search, value: string) => {
    void navigate({
      to: "/tournaments",
      search: (current: Search) => ({ ...current, [name]: value || undefined }),
      replace: true,
    });
  };

  const day = (value: string | null) =>
    value === null ? t("tournaments.unknown") : new Date(value).toLocaleDateString(locale);

  return (
    <>
      <h2>{t("tournaments.title")}</h2>

      {/* A64-024.5H. The created tournament is `draft`, which the default
          list shows — so the page is re-read rather than having a row
          spliced in from a response that carries two fields. */}
      <CreateTournament onCreated={() => void reload()} />

      <div className="filters">
        <p className="field">
          <label htmlFor="tournament-status">{t("tournaments.status")}</label>
          <select
            id="tournament-status"
            value={search.status ?? ""}
            onChange={(event) => setFilter("status", event.target.value)}
          >
            <option value="">{t("tournaments.any")}</option>
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </p>

        <p className="field">
          <label htmlFor="tournament-rated">{t("tournaments.mode")}</label>
          <select
            id="tournament-rated"
            value={search.rated ?? ""}
            onChange={(event) => setFilter("rated", event.target.value)}
          >
            <option value="">{t("tournaments.any")}</option>
            <option value="true">{t("tournaments.rated")}</option>
            <option value="false">{t("tournaments.casual")}</option>
          </select>
        </p>
      </div>

      {state === "loading" && <p role="status">{t("tournaments.loading")}</p>}
      {state === "error" && (
        <p role="alert" className="error">
          {t("tournaments.error")}
        </p>
      )}
      {state === "ready" && rows.length === 0 && (
        <>
          <p role="status">{t("tournaments.empty")}</p>
          <p className="muted">{t("tournaments.emptyHint")}</p>
        </>
      )}

      {state === "ready" && rows.length > 0 && (
        <>
          <table className="users-table">
            <thead>
              <tr>
                <th scope="col">{t("tournaments.colName")}</th>
                <th scope="col">{t("tournaments.colStatus")}</th>
                <th scope="col">{t("tournaments.colFormat")}</th>
                <th scope="col">{t("tournaments.colEntrants")}</th>
                <th scope="col">{t("tournaments.colVariant")}</th>
                <th scope="col">{t("tournaments.colStart")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((tournament) => (
                <tr key={tournament.tournament_id}>
                  <td>
                    <Link
                      to="/tournaments/$tournamentId"
                      params={{ tournamentId: tournament.tournament_id }}
                    >
                      {tournament.name}
                    </Link>
                  </td>
                  <td>{tournament.status}</td>
                  <td>{tournament.format}</td>
                  <td>
                    {tournament.entrant_count} / {tournament.capacity}
                  </td>
                  <td>{tournament.variant}</td>
                  <td>{day(tournament.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <ul className="users-cards">
            {rows.map((tournament) => (
              <li key={tournament.tournament_id}>
                <Link
                  to="/tournaments/$tournamentId"
                  params={{ tournamentId: tournament.tournament_id }}
                >
                  {tournament.name}
                </Link>
                <span>
                  {tournament.status} · {tournament.format} · {tournament.entrant_count}/
                  {tournament.capacity}
                </span>
                <span className="muted">
                  {tournament.variant} · {day(tournament.started_at)}
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
                {t(loadingMore ? "tournaments.loadingMore" : "tournaments.more")}
              </button>
            </p>
          )}

          {moreFailed && (
            <p role="alert" className="error">
              {t("tournaments.moreError")}
            </p>
          )}
        </>
      )}
    </>
  );
}
