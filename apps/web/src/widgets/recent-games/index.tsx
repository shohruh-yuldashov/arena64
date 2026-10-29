import type { ReactNode } from "react";

import { useRecentMatches } from "@/features/match-history/model/queries";
import { MatchRow } from "@/features/match-history/ui/match-row";
import { useTranslation } from "@/shared/i18n";
import { ListState } from "@/shared/ui";

/**
 * A player's last few finished matches, on their profile — A64-032.
 *
 * The rows are `MatchRow`, the history page's own, so a match reads the
 * same in both places and links to the same replay. They are read from the
 * profile owner's seat: "Won" means *they* won, and the opponent is
 * *theirs*, whoever is looking.
 *
 * ## What is listed is the server's decision
 *
 * Rated matches are public; a casual one reaches only somebody who played
 * in it (`specs/replay.md` §3). Nothing here filters, hides or explains a
 * gap — a client that second-guessed the rule would be a second answer to
 * it. The rating change appears only on the owner's own profile, because
 * that is the only time the server sends it.
 *
 * `action` is a slot rather than a flag: the owner's profile links to the
 * full history, and somebody else's has nowhere to send them — there is no
 * page for another player's whole record, and inventing one is A64-034's.
 */
export function RecentGames({
  playerId,
  action,
}: {
  playerId: string | undefined;
  action?: ReactNode;
}) {
  const { t } = useTranslation();
  const recent = useRecentMatches(playerId);
  const entries = recent.data?.entries ?? [];

  return (
    <section aria-labelledby="recent-games-heading" className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between gap-3">
        <h2 id="recent-games-heading" className="text-base font-semibold">
          {t("profile.recentGames.title")}
        </h2>
        {entries.length > 0 && action}
      </div>

      <ListState
        isPending={recent.isPending}
        isError={recent.isError}
        isEmpty={entries.length === 0}
        loadingLabel={t("profile.recentGames.loadingLabel")}
        errorMessage={t("profile.recentGames.error")}
        emptyTitle={t("profile.recentGames.empty")}
        pendingRows={3}
        pendingRowClassName="h-14 rounded-xl"
        onRetry={() => void recent.refetch()}
      >
        {entries.length > 0 && playerId !== undefined && (
          <ul className="border-border bg-card divide-border divide-y overflow-hidden rounded-xl border">
            {entries.map((entry) => (
              <MatchRow key={entry.match_id} entry={entry} playerId={playerId} />
            ))}
          </ul>
        )}
      </ListState>
    </section>
  );
}
