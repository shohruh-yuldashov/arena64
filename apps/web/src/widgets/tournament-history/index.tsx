import { Link } from "@tanstack/react-router";

import { useTournamentHistory } from "@/features/profile/model/queries";
import { useTranslation } from "@/shared/i18n";
import { formatDate } from "@/shared/lib/format";
import { Button, Skeleton, Spinner } from "@/shared/ui";

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

      {history.isPending && (
        <div className="flex flex-col gap-2" aria-hidden="true">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      )}

      {history.isSuccess && entries.length === 0 && (
        <p className="text-muted-foreground text-sm">{t("profile.tournaments.empty")}</p>
      )}

      {entries.length > 0 && (
        <ul className="flex flex-col gap-2">
          {entries.map((entry) => (
            <li
              key={entry.tournament.id}
              className="border-border flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 rounded-md border px-3 py-2"
            >
              {/* A64-020.6 §20. The row already holds the whole summary, so
                  linking costs **no** request — nothing is prefetched and no
                  detail is read per row. Without it the bracket a player
                  competed in is reachable only from the lobby. */}
              <Link
                to="/tournaments/$tournamentId"
                params={{ tournamentId: entry.tournament.id }}
                className="text-sm font-medium underline-offset-4 hover:underline focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none"
              >
                {entry.tournament.name}
              </Link>
              <span className="text-muted-foreground text-xs">
                {formatDate(entry.tournament.created_at, locale)}
              </span>
              <span className="text-sm tabular-nums">
                {entry.final_rank === null || entry.final_rank === undefined
                  ? t("profile.tournaments.inProgress")
                  : t("profile.tournaments.rank", { rank: entry.final_rank })}
              </span>
            </li>
          ))}
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
    </section>
  );
}
