import { useState } from "react";

import type { TournamentFilters, TournamentStatus } from "@/features/tournament/api";
import { useTournaments } from "@/features/tournament/model/queries";
import { TournamentCard } from "@/features/tournament/ui/tournament-card";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { Button, Skeleton } from "@/shared/ui";

/**
 * The tournament lobby — A64-020.6 §5, §6, §22.
 *
 * ## Filters exist because the backend has them
 *
 * `GET /tournaments` accepts a closed set of five — status, format,
 * variant, speed class and rated — each an enum or a boolean over an
 * indexed column. This surfaces the one a player actually navigates by:
 * *what can I do with this tournament right now*.
 *
 * §6 is explicit that a filter without a backend contract must be omitted
 * rather than faked, and that fetching every page to filter locally is
 * forbidden. Both matter here: a client-side "registration open" filter
 * over one page of twenty would hide open tournaments that happened to be
 * on page two and would look, from the outside, exactly like a lobby with
 * nothing in it.
 *
 * Format, variant and speed class are **not** surfaced. The platform runs
 * one format and one variant today, so three controls whose every option
 * returns the same list is furniture. They are one parameter away when a
 * second value exists.
 *
 * There is no search: the endpoint has no free-text contract, and §6
 * forbids expanding the API for a decorative control.
 *
 * ## Server ordering, never client
 *
 * Newest first, by keyset. Nothing here re-sorts: a sort applied to the
 * loaded pages would order twenty rows and present it as the ordering of
 * the whole lobby.
 *
 * ## No "create tournament"
 *
 * §22. Creation is an operator command with no player-facing endpoint, and
 * a button that could only ever fail is worse than no button.
 */

interface View {
  id: string;
  status?: TournamentStatus;
  label: TranslationKey;
}

/** The four views, each a real `status` value or the absence of one. */
const ALL_TOURNAMENTS: View = { id: "all", label: "tournament.filter.all" };
const VIEWS: View[] = [
  ALL_TOURNAMENTS,
  { id: "open", status: "registration_open", label: "tournament.filter.open" },
  { id: "in_progress", status: "in_progress", label: "tournament.filter.in_progress" },
  { id: "completed", status: "completed", label: "tournament.filter.completed" },
];

export default function TournamentsPage() {
  const { t } = useTranslation();
  const [view, setView] = useState(ALL_TOURNAMENTS.id);

  const selected = VIEWS.find((candidate) => candidate.id === view) ?? ALL_TOURNAMENTS;
  const filters: TournamentFilters =
    selected.status !== undefined ? { status: selected.status } : {};

  const { data, isPending, isError, refetch, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useTournaments(filters);

  const tournaments = data?.pages.flatMap((page) => page.entries) ?? [];
  const isFiltered = selected.status !== undefined;

  return (
    <section className="mx-auto flex w-full max-w-3xl flex-col gap-4 py-6">
      <h1 className="text-xl font-semibold">{t("tournament.title")}</h1>

      {/* A radio group rather than buttons: the four views are mutually
          exclusive, and a screen reader should announce which one is
          selected — §24. */}
      <div
        role="radiogroup"
        aria-label={t("tournament.filter.legend")}
        className="flex flex-wrap gap-2"
      >
        {VIEWS.map((candidate) => (
          <button
            key={candidate.id}
            type="button"
            role="radio"
            aria-checked={candidate.id === view}
            onClick={() => setView(candidate.id)}
            className={cn(
              "border-border focus-visible:ring-ring min-h-11 rounded-full border px-4 text-sm",
              "focus-visible:ring-2 focus-visible:outline-none",
              candidate.id === view && "border-primary text-primary font-medium",
            )}
          >
            {t(candidate.label)}
          </button>
        ))}
      </div>

      {isPending && (
        <div className="flex flex-col gap-3">
          <span role="status" className="sr-only">
            {t("tournament.loading")}
          </span>
          {[0, 1, 2].map((row) => (
            <Skeleton key={row} className="h-24 w-full" />
          ))}
        </div>
      )}

      {isError && (
        <div role="alert" className="flex flex-col items-start gap-3">
          <p className="text-sm">{t("tournament.listError")}</p>
          <Button variant="outline" className="min-h-11" onClick={() => void refetch()}>
            {t("common.retry")}
          </Button>
        </div>
      )}

      {!isPending && !isError && tournaments.length === 0 && (
        <div className="flex flex-col items-start gap-3 py-8">
          <p className="font-medium">{t("tournament.empty.title")}</p>
          <p className="text-muted-foreground text-sm">
            {t(isFiltered ? "tournament.empty.filtered" : "tournament.empty.body")}
          </p>
          {isFiltered && (
            <Button
              variant="outline"
              className="min-h-11"
              onClick={() => setView(ALL_TOURNAMENTS.id)}
            >
              {t("tournament.empty.clearFilter")}
            </Button>
          )}
        </div>
      )}

      {tournaments.length > 0 && (
        <>
          <ol aria-label={t("tournament.title")} className="flex flex-col gap-3">
            {tournaments.map((tournament) => (
              <TournamentCard key={tournament.id} tournament={tournament} />
            ))}
          </ol>

          {hasNextPage ? (
            // Focus stays on this button while the next page loads — §24.
            <Button
              variant="outline"
              className="min-h-11 self-center"
              disabled={isFetchingNextPage}
              onClick={() => void fetchNextPage()}
            >
              {t(isFetchingNextPage ? "tournament.loadingMore" : "tournament.loadMore")}
            </Button>
          ) : (
            <p role="status" className="text-muted-foreground py-2 text-center text-sm">
              {t("tournament.end")}
            </p>
          )}
        </>
      )}
    </section>
  );
}
