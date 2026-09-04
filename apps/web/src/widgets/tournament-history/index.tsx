import { Link } from "@tanstack/react-router";

import { speedClassKey } from "@/entities/time-control";
import { useTournamentHistory } from "@/features/profile/model/queries";
import { finalStatusKey, formatKey } from "@/features/tournament/ui/labels";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatDate } from "@/shared/lib/format";
import { speedAccent } from "@/shared/lib/speed-accent";
import { Button, ListState, Spinner } from "@/shared/ui";

/**
 * A player's tournaments, newest first.
 *
 * ## One request per page, never one per row
 *
 * `GET /players/{id}/tournaments` returns each entry's summary in the same
 * statement (A64-020.0C removed the N+1 that used to be there). So a row is
 * rendered from the page it arrived in, and there is deliberately no
 * per-row query — reintroducing it here would put the defect back on the
 * client side of the same boundary.
 *
 * ## Keyset, so "load more" and not "page 3"
 *
 * The API issues an opaque cursor and this sends it back unread. There is
 * no page number to jump to, which is the point: a history grows while it
 * is read, and an offset would show a tournament twice or skip it.
 *
 * ## Two nulls that mean different things
 *
 * `final_rank` is `null` while a tournament is still being played — that is
 * "in progress", not "unplaced". Ranks are also **not dense**: two players
 * knocked out in the same round share one, so an eight-player bracket has
 * no fourth place. Renumbering them would publish a comparison nobody made.
 *
 * ## What a row says — A64-025.9 §18.8
 *
 * It said three things at one weight: a name, a date, and a rank, spread by
 * `justify-between` so the date floated in the middle of the row with
 * nothing to align to. The response already carried the rest of the
 * summary — the speed class, the format, and `final_status` — and none of
 * it was rendered, which is why "Rank 1" and "Champion" were the same
 * information printed once, in the weaker of the two forms.
 *
 * Now: the placing leads, as a chip; the name is the row; the speed class,
 * format and date are one subordinate line; and the outcome is stated in
 * words on the right. Gold marks a win — and the word "Champion" is beside
 * it, because colour is never the only signal (WCAG 1.4.1).
 *
 * ## The failure state this never had — A64-025.11 §32
 *
 * There was no `isError` branch. A failed request rendered **nothing**,
 * under a heading that says "Tournament history", which a player reads as
 * "you have not played in any" — a broken list looking exactly like a
 * healthy empty one. The loading state was `aria-hidden`, so a screen
 * reader was told nothing either.
 *
 * Both are `ListState`'s now, which is the argument for a component over a
 * convention: a convention can be forgotten silently, and this is what that
 * looks like.
 */
export function TournamentHistory({ playerId }: { playerId: string | undefined }) {
  const { t, locale } = useTranslation();
  const history = useTournamentHistory(playerId);

  const entries = history.data?.pages.flatMap((page) => page.entries) ?? [];

  return (
    <section aria-labelledby="tournaments-heading" className="flex flex-col gap-3">
      <h2 id="tournaments-heading" className="text-base font-semibold">
        {t("profile.tournaments.title")}
      </h2>

      <ListState
        isPending={history.isPending}
        isError={history.isError}
        isEmpty={entries.length === 0}
        loadingLabel={t("profile.tournaments.loadingLabel")}
        errorMessage={t("profile.tournaments.error")}
        emptyTitle={t("profile.tournaments.empty")}
        pendingRows={2}
        pendingRowClassName="h-16 rounded-xl"
        onRetry={() => void history.refetch()}
      >
        {entries.length > 0 && (
          <ul className="border-border bg-card divide-border divide-y overflow-hidden rounded-xl border">
            {entries.map((entry) => {
              const rank = entry.final_rank ?? null;
              const status = entry.final_status ?? null;
              const accent = speedAccent(entry.tournament.speed_class);

              return (
                <li
                  key={entry.tournament.id}
                  // The chip stays on the left at every width; the name, the
                  // meta line and the outcome stack under each other below
                  // `sm` and spread across above it. `flex-wrap` alone kept
                  // the outcome on the first line, where it squeezed a
                  // tournament's name down to "Autumn Blitz C…" at 360.
                  className="flex items-start gap-3 px-4 py-3 sm:items-center sm:gap-4 sm:px-5"
                >
                  <span
                    className={cn(
                      // A fixed width, not `min-w-`: "Rank 4" and the em dash
                      // are different lengths, and a chip that sizes to its
                      // own text starts each name at a different x.
                      "inline-flex h-8 w-20 shrink-0 items-center justify-center rounded-md px-2 text-xs font-semibold tabular-nums",
                      rank === 1
                        ? "bg-rating/20 text-foreground"
                        : "bg-muted text-muted-foreground",
                    )}
                  >
                    {rank === null ? "—" : t("profile.tournaments.rank", { rank })}
                  </span>

                  <div className="flex min-w-0 flex-1 flex-col gap-1 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
                    <div className="flex min-w-0 flex-col gap-0.5">
                      {/* A64-020.6 §20. The row already holds the whole summary,
                      so linking costs **no** request — nothing is prefetched
                      and no detail is read per row. Without it the bracket a
                      player competed in is reachable only from the lobby. */}
                      <Link
                        to="/tournaments/$tournamentId"
                        params={{ tournamentId: entry.tournament.id }}
                        className="focus-visible:ring-ring truncate text-sm font-medium underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:outline-none"
                      >
                        {entry.tournament.name}
                      </Link>

                      <div className="text-muted-foreground flex flex-wrap items-center gap-x-1.5 text-xs [&>span:not(:last-child)]:after:ml-1.5 [&>span:not(:last-child)]:after:content-['·']">
                        <span className={cn("font-medium", accent.text)}>
                          {t(speedClassKey(entry.tournament.speed_class))}
                        </span>
                        <span>{t(formatKey(entry.tournament.format))}</span>
                        <span>{formatDate(entry.tournament.created_at, locale)}</span>
                      </div>
                    </div>

                    <span
                      className={cn(
                        "shrink-0 text-sm font-medium",
                        status === null && "text-muted-foreground",
                        status === "champion" && "text-rating font-semibold",
                      )}
                    >
                      {status === null
                        ? t("profile.tournaments.inProgress")
                        : t(finalStatusKey(status))}
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {history.hasNextPage && (
          <Button
            variant="outline"
            className="self-start"
            disabled={history.isFetchingNextPage}
            onClick={() => void history.fetchNextPage()}
          >
            {history.isFetchingNextPage ? (
              <Spinner label={t("profile.tournaments.loading")} />
            ) : (
              t("profile.tournaments.loadMore")
            )}
          </Button>
        )}
      </ListState>
    </section>
  );
}
