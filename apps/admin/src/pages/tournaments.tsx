import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback } from "react";

import {
  type AdminTournamentSummary,
  fetchTournaments,
  type TournamentQuery,
} from "@/shared/api/client";
import { CreateTournament } from "@/features/tournaments/create-tournament";
import { useTranslation } from "@/shared/i18n";
import { ErrorNotice } from "@/shared/ui/error-notice";
import { PageHeader } from "@/shared/ui/page-header";
import { Pagination } from "@/shared/ui/pagination";
import { useCursorPages } from "@/shared/ui/use-cursor-pages";

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

  const query: TournamentQuery = {
    ...(search.status ? { status: search.status } : {}),
    ...(tri(search.rated) !== undefined ? { rated: tri(search.rated) } : {}),
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
  const pages = useCursorPages<AdminTournamentSummary>(
    useCallback(
      (cursor, signal) => fetchTournaments({ ...query, ...(cursor ? { cursor } : {}) }, signal),
      [key],
    ),
    key,
  );

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
      {/* The create control sits beside the heading rather than below the
          filters: it is what an operator came to this page to do, and a
          primary action under a filter row reads as part of the filter. */}
      <PageHeader
        title={t("tournaments.title")}
        actions={<CreateTournament onCreated={() => pages.reload()} />}
      />

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

      {pages.state === "loading" && <p role="status">{t("tournaments.loading")}</p>}
      {pages.state === "error" && <ErrorNotice message={t("tournaments.error")} />}
      {pages.state === "ready" && pages.rows.length === 0 && (
        <>
          <p role="status">{t("tournaments.empty")}</p>
          <p className="muted">{t("tournaments.emptyHint")}</p>
        </>
      )}

      {pages.state === "ready" && pages.rows.length > 0 && (
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
              {pages.rows.map((tournament) => (
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
            {pages.rows.map((tournament) => (
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
