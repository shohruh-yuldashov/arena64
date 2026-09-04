import { Link } from "@tanstack/react-router";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { useMatchHistory } from "@/features/match-history/model/queries";
import { MatchRow } from "@/features/match-history/ui/match-row";
import { useTranslation } from "@/shared/i18n";
import { Button, ListState } from "@/shared/ui";

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
      {/* A64-025.5D. "Back to profile" sat here and is gone: match history
          is a section in the header at every width, so a button pointing at
          the profile duplicated a route the shell already offers and made
          the page look like a sub-page of one profile. The wrapper went with
          it — a `justify-between` row with one child is a row for nothing. */}
      <h1 className="text-xl font-semibold">{t("history.title")}</h1>

      {/* A64-025.11 §32. This page wrote all three branches itself — its own
          skeleton, its own `Notice`, its own empty block — and each was
          slightly different from the same branch on `/tournaments` next
          door. `ListState` owns them now.

          §21 for the empty state: helpful, not "No data". A player with no
          history is a player who has not played yet, and the useful thing to
          give them is the way to. */}
      <ListState
        isPending={isPending}
        isError={isError}
        isEmpty={entries.length === 0}
        loadingLabel={t("history.loading")}
        errorMessage={t("history.error")}
        emptyTitle={t("history.empty.title")}
        emptyHint={t("history.empty.body")}
        emptyAction={
          <Button asChild className="min-h-11">
            <Link to="/play">{t("history.empty.action")}</Link>
          </Button>
        }
        pendingRows={4}
        pendingRowClassName="h-14"
        onRetry={() => void refetch()}
      >
        {viewerId !== null && (
          <>
            {/* A semantic list — §23. Each row is an item, so a screen reader
              announces the count and the position without being told.

              In a card since A64-025.5C §23: it was the last list in the
              product still floating on the page background with hairline
              rules and nothing containing it. */}
            <ol
              aria-label={t("history.title")}
              className="border-border bg-card divide-border flex flex-col divide-y overflow-hidden rounded-xl border"
            >
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
      </ListState>
    </section>
  );
}
