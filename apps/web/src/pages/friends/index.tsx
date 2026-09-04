import { Link } from "@tanstack/react-router";

import { ChallengeButton } from "@/features/challenges/ui/challenge-button";
import { useFriendCount, useFriends } from "@/features/social/model/queries";
import { RelationshipActions } from "@/features/social/ui/relationship-actions";
import { useTranslation } from "@/shared/i18n";
import { formatDate, formatNumber } from "@/shared/lib/format";
import { ListState } from "@/shared/ui";
import { Button, Spinner } from "@/shared/ui";
import { PlayerRow } from "@/widgets/player-row";
import { SocialNav } from "@/widgets/social-nav";

/**
 * `/friends` — the friends list.
 *
 * ## One request per page, never one per friend
 *
 * `GET /friends` returns each friend's whole public profile in the page —
 * composed server-side in a fixed number of statements — so a row needs no
 * follow-up call. A per-row profile fetch would reintroduce on the client
 * exactly the N+1 the composer exists to avoid.
 *
 * ## Presence is what the server sent
 *
 * Friends see fields restricted to friends, which is why a friend's
 * `is_online` is often present here and absent on a stranger's search
 * result — that is `VisibilityLevel.FRIENDS` working end to end, and
 * `PlayerRow` renders whichever fields arrived without defaulting any.
 *
 * Ordering is the backend's: most recently added first, keyset-paged.
 */
export default function FriendsPage() {
  const { t, locale } = useTranslation();
  const friends = useFriends();
  const count = useFriendCount();

  const items = friends.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <SocialNav
      title={t("social.friends.title")}
      description={
        count.data !== undefined
          ? t("social.friends.count", { count: formatNumber(count.data.total, locale) })
          : undefined
      }
    >
      <div className="flex flex-col gap-4">
        <ListState
          isPending={friends.isPending}
          isError={friends.isError}
          isEmpty={items.length === 0}
          emptyTitle={t("social.friends.empty")}
          emptyHint={t("social.friends.emptyHint")}
          // The hint says to find players; this is the way to. It was in
          // the navigation beside it and nowhere the sentence pointed.
          emptyAction={
            <Button asChild variant="outline">
              <Link to="/search">{t("social.nav.search")}</Link>
            </Button>
          }
          onRetry={() => void friends.refetch()}
        >
          <ul
            aria-label={t("social.friends.title")}
            className="border-border bg-card divide-border flex flex-col divide-y overflow-hidden rounded-xl border"
          >
            {items.map((entry) => (
              <PlayerRow
                key={entry.player.id}
                player={entry.player}
                meta={t("social.friends.since", {
                  date: formatDate(entry.friends_since, locale) ?? "",
                })}
                actions={
                  // A64-022.5 §14. The challenge sits **before** the
                  // relationship actions, because it is the thing somebody
                  // came to a friends list to do — and because the actions
                  // beside it are removal and blocking, which should not be
                  // the first controls under a cursor.
                  <div className="flex flex-wrap items-center gap-2">
                    <ChallengeButton
                      playerId={entry.player.id}
                      playerName={entry.player.display_name ?? entry.player.username}
                      state={entry.player.relationship}
                    />
                    <RelationshipActions
                      playerId={entry.player.id}
                      playerName={entry.player.display_name ?? entry.player.username}
                      state={entry.player.relationship}
                    />
                  </div>
                }
              />
            ))}
          </ul>
        </ListState>

        {friends.hasNextPage && (
          <Button
            variant="outline"
            className="min-h-11 self-start"
            disabled={friends.isFetchingNextPage}
            onClick={() => void friends.fetchNextPage()}
          >
            {friends.isFetchingNextPage ? (
              <Spinner label={t("state.loading")} />
            ) : (
              t("social.friends.loadMore")
            )}
          </Button>
        )}
      </div>
    </SocialNav>
  );
}
