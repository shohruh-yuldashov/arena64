import { useBlockedPlayers } from "@/features/social/model/queries";
import { ListState } from "@/features/social/ui/list-state";
import { RelationshipActions } from "@/features/social/ui/relationship-actions";
import { useTranslation } from "@/shared/i18n";
import { formatDate } from "@/shared/lib/format";
import { Button, Spinner } from "@/shared/ui";
import { PlayerRow } from "@/widgets/player-row";
import { SocialNav } from "@/widgets/social-nav";

/**
 * `/friends/blocked` — who the viewer has blocked.
 *
 * ## Only blocks the viewer placed
 *
 * `GET /blocks` returns those and nothing else — a block placed *on* the
 * viewer never appears here or anywhere, which is the whole reason a block
 * is worth placing. So this page cannot reveal whether the blocked player
 * also blocked back, because the data to reveal it does not exist.
 *
 * ## The profile link stays
 *
 * A blocked player's profile is still reachable *to their blocker* — the
 * API composes it as they could see it before blocking. Removing the link
 * would make the list unusable for the one thing it is for: recognising
 * somebody before unblocking them.
 *
 * Every row's state is `blocked`, so `RelationshipActions` offers exactly
 * one control — unblock — and no path here can add a friend.
 */
export default function BlockedPage() {
  const { t, locale } = useTranslation();
  const blocked = useBlockedPlayers();
  const items = blocked.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <SocialNav title={t("social.blocked.title")}>
      <div className="flex flex-col gap-4">
        <ListState
          isPending={blocked.isPending}
          isError={blocked.isError}
          isEmpty={items.length === 0}
          emptyTitle={t("social.blocked.empty")}
          emptyHint={t("social.blocked.emptyHint")}
          onRetry={() => void blocked.refetch()}
        >
          <ul aria-label={t("social.blocked.title")} className="flex flex-col gap-2">
            {items.map((entry) => (
              <PlayerRow
                key={entry.player.id}
                player={entry.player}
                meta={t("social.blocked.since", {
                  date: formatDate(entry.blocked_at, locale) ?? "",
                })}
                actions={
                  <RelationshipActions
                    playerId={entry.player.id}
                    playerName={entry.player.display_name ?? entry.player.username}
                    state={entry.player.relationship}
                  />
                }
              />
            ))}
          </ul>
        </ListState>

        {blocked.hasNextPage && (
          <Button
            variant="outline"
            className="min-h-11 self-start"
            disabled={blocked.isFetchingNextPage}
            onClick={() => void blocked.fetchNextPage()}
          >
            {blocked.isFetchingNextPage ? (
              <Spinner label={t("social.state.loading")} />
            ) : (
              t("social.blocked.loadMore")
            )}
          </Button>
        )}
      </div>
    </SocialNav>
  );
}
