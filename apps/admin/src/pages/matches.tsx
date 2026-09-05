import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback } from "react";

import { type AdminMatchSummary, fetchMatches, type MatchQuery } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { useVocab } from "@/features/vocabulary";
import { DataTable } from "@/shared/ui/data-table";
import { FilterToolbar, SearchField, SelectField } from "@/shared/ui/filter-toolbar";
import { StatusBadge, type Tone } from "@/shared/ui/status-badge";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/shared/ui/states";
import { PageHeader } from "@/shared/ui/page-header";
import { Pagination } from "@/shared/ui/pagination";
import { useCursorPages } from "@/shared/ui/use-cursor-pages";

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

/**
 * How each state reads at a glance — A64-027A §9, §24.
 *
 * The hue is an accelerant for somebody scanning a hundred rows. The
 * translated word beside it is what carries the meaning, which is why
 * the enum itself never reaches the screen: an administrator who did
 * not build Arena64 should not have to decode `registration_open`.
 */
const TONES: Record<string, Tone> = {
  active: "success",
  completed: "neutral",
  pending_acceptance: "info",
  declined: "danger",
  expired: "warning",
};

type Search = { status?: string; rated?: string; origin?: string; participant?: string };

function tri(value: string | undefined): boolean | undefined {
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

export function MatchesPage() {
  const { t, locale } = useTranslation();
  const vocab = useVocab();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as Search;

  const query: MatchQuery = {
    ...(search.status ? { status: search.status } : {}),
    ...(tri(search.rated) !== undefined ? { rated: tri(search.rated) } : {}),
    ...(search.origin ? { origin: search.origin } : {}),
    ...(search.participant ? { participant_id: search.participant } : {}),
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
  const pages = useCursorPages<AdminMatchSummary>(
    useCallback(
      (cursor, signal) => fetchMatches({ ...query, ...(cursor ? { cursor } : {}) }, signal),
      [key],
    ),
    key,
  );

  const setFilter = (name: keyof Search, value: string) => {
    void navigate({
      to: "/matches",
      search: (current: Search) => ({ ...current, [name]: value || undefined }),
      replace: true,
    });
  };

  const seat = (player: AdminMatchSummary["light"]) =>
    player.display_name ?? player.username ?? player.player_id.slice(0, 8);

  /** The result, as a sentence. Never the raw `outcome` enum — §40. */
  const result = (match: AdminMatchSummary) => {
    // The side that won, said as a sentence. "Light" alone is a fact the
    // reader has to finish themselves.
    if (match.winner !== null) return t("matches.wonBy", { side: vocab("side", match.winner) });
    if (match.outcome !== null) return vocab("outcome", match.outcome);
    return t("matches.noResult");
  };

  return (
    <>
      <PageHeader title={t("matches.title")} description={t("matches.lede")} />

      <FilterToolbar
        activeCount={
          [search.status, search.rated, search.origin, search.participant].filter(Boolean)
            .length
        }
        onClear={() => {
          void navigate({ to: "/matches", search: {}, replace: true });
        }}
        search={
          <SearchField
            label={t("matches.participant")}
            hint={t("matches.participantHint")}
            value={search.participant ?? ""}
            onChange={(value) => {
              setFilter("participant", value.trim());
            }}
          />
        }
        filters={
          <>
            <SelectField
              label={t("matches.status")}
              value={search.status ?? ""}
              onChange={(value) => {
                setFilter("status", value);
              }}
            >
              <option value="">{t("matches.any")}</option>
              {STATUSES.map((value) => (
                <option key={value} value={value}>
                  {t(`matches.statusLabel.${value}` as "matches.statusLabel.completed")}
                </option>
              ))}
            </SelectField>

            <SelectField
              label={t("matches.ratedLabel")}
              value={search.rated ?? ""}
              onChange={(value) => {
                setFilter("rated", value);
              }}
            >
              <option value="">{t("matches.any")}</option>
              <option value="true">{t("matches.rated")}</option>
              <option value="false">{t("matches.casual")}</option>
            </SelectField>

            <SelectField
              label={t("matches.origin")}
              value={search.origin ?? ""}
              onChange={(value) => {
                setFilter("origin", value);
              }}
            >
              <option value="">{t("matches.any")}</option>
              {ORIGINS.map((value) => (
                <option key={value} value={value}>
                  {vocab("matchOrigin", value)}
                </option>
              ))}
            </SelectField>
          </>
        }
      />

      {pages.state === "loading" && <LoadingSkeleton label={t("matches.loading")} />}
      {pages.state === "error" && (
        <ErrorState title={t("matches.error")} onRetry={pages.reload} />
      )}

      {pages.state === "ready" && pages.rows.length === 0 && (
        <EmptyState
          icon="matches"
          title={t("matches.empty")}
          description={t("matches.emptyHint")}
        />
      )}

      {pages.state === "ready" && pages.rows.length > 0 && (
        <>
          <DataTable caption={t("matches.title")} minWidth="52rem">
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
              {pages.rows.map((match) => (
                <tr key={match.match_id}>
                  <th scope="row">
                    {/* A matchup, not a hyphenated pair. The two seats are
                        stacked with the side they played, so a row reads as
                        a game rather than as two names. */}
                    <Link
                      className="matchup"
                      to="/matches/$matchId"
                      params={{ matchId: match.match_id }}
                    >
                      <span className="matchup__seat">
                        <span className="matchup__pip" data-side="light" aria-hidden="true" />
                        <span>{seat(match.light)}</span>
                      </span>
                      <span className="matchup__vs">{t("matches.versus")}</span>
                      <span className="matchup__seat">
                        <span className="matchup__pip" data-side="dark" aria-hidden="true" />
                        <span>{seat(match.dark)}</span>
                      </span>
                    </Link>
                  </th>
                  <td>
                    <StatusBadge
                      label={t(
                        `matches.statusLabel.${match.status}` as "matches.statusLabel.completed",
                      )}
                      tone={TONES[match.status] ?? "neutral"}
                    />
                  </td>
                  <td>{result(match)}</td>
                  <td>{t(match.rated ? "matches.rated" : "matches.casual")}</td>
                  <td>{vocab("matchOrigin", match.origin)}</td>
                  <td>{new Date(match.created_at).toLocaleDateString(locale)}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>

          {/* Narrow: the same rows as cards, nothing dropped. */}
          <ul className="users-cards">
            {pages.rows.map((match) => (
              <li key={match.match_id}>
                <Link to="/matches/$matchId" params={{ matchId: match.match_id }}>
                  {seat(match.light)} — {seat(match.dark)}
                </Link>
                <span>
                  {t(`matches.statusLabel.${match.status}` as "matches.statusLabel.completed")}{" "}
                  · {result(match)} · {t(match.rated ? "matches.rated" : "matches.casual")}
                </span>
                <span className="muted">
                  {vocab("matchOrigin", match.origin)} ·{" "}
                  {new Date(match.created_at).toLocaleDateString(locale)}
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
