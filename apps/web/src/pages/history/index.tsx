import { Link } from "@tanstack/react-router";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { useMatchHistory } from "@/features/match-history/model/queries";
import { MatchRow } from "@/features/match-history/ui/match-row";
import { useTranslation } from "@/shared/i18n";
import { Button, Skeleton } from "@/shared/ui";

/**
 * The player's finished matches — A64-020.5F §15, §21, §24.
 *
 * ## Whose history, and why the route takes no parameter
 *
 * The authenticated player's. §15's minimum scope is the current player,
 * and the id comes from the session rather than the URL — so there is no
 * parameter to tamper with and no question about whose record this is.
 *
 * A public `/players/$username/games` would be a different surface with a
 * different privacy answer (casual matches are private; rated ones are
 * not), and §15 asks for it only if navigation clearly requires it. It does
 * not yet: nothing links to another player's history.
 *
 * ## One request per page, none per row
 *
 * The opponent and the time control are composed by the backend, so a row
 * renders from what the page gave it. There is no per-entry query in this
 * tree to make one.
 */
export default function HistoryPage() {
  const { t } = useTranslation();
  const { state: session } = useSession();
  const viewerId = isAuthenticated(session) ? session.user.id : null;

  const { data, isPending, isError, refetch, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useMatchHistory(viewerId);

  const entries = data?.pages.flatMap((page) => page.entries) ?? [];

  return (
    <section className="mx-auto flex w-full max-w-3xl flex-col gap-4 py-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">{t("history.title")}</h1>
        <Button asChild variant="outline" className="min-h-11">
          <Link to="/profile">{t("history.backToProfile")}</Link>
        </Button>
      </div>

      {isPending && (
        <div className="flex flex-col gap-3">
          <span role="status" className="sr-only">
            {t("history.loading")}
          </span>
          {[0, 1, 2, 3].map((row) => (
            <Skeleton key={row} className="h-14 w-full" />
          ))}
        </div>
      )}

      {isError && (
        <div role="alert" className="flex flex-col items-start gap-3">
          <p className="text-sm">{t("history.error")}</p>
          <Button variant="outline" className="min-h-11" onClick={() => void refetch()}>
            {t("common.retry")}
          </Button>
        </div>
      )}

      {!isPending && !isError && entries.length === 0 && (
        // §21: helpful, not "No data". A player with no history is a player
        // who has not played yet, and the useful thing to give them is the
        // way to.
        <div className="flex flex-col items-start gap-3 py-8">
          <p className="font-medium">{t("history.empty.title")}</p>
          <p className="text-muted-foreground text-sm">{t("history.empty.body")}</p>
          <Button asChild className="min-h-11">
            <Link to="/play">{t("history.empty.action")}</Link>
          </Button>
        </div>
      )}

      {entries.length > 0 && viewerId !== null && (
        <>
          {/* A semantic list — §23. Each row is an item, so a screen reader
              announces the count and the position without being told. */}
          <ol aria-label={t("history.title")} className="flex flex-col">
            {entries.map((entry) => (
              <MatchRow key={entry.match_id} entry={entry} viewerId={viewerId} />
            ))}
          </ol>

          {hasNextPage ? (
            // Focus stays on this button while the next page loads, which
            // is why it is not replaced by a spinner — §23's "focus does
            // not jump when loading more".
            <Button
              variant="outline"
              className="min-h-11 self-center"
              disabled={isFetchingNextPage}
              onClick={() => void fetchNextPage()}
            >
              {t(isFetchingNextPage ? "history.loadingMore" : "history.loadMore")}
            </Button>
          ) : (
            <p role="status" className="text-muted-foreground py-2 text-center text-sm">
              {t("history.end")}
            </p>
          )}
        </>
      )}
    </section>
  );
}
