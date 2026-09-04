import { useIncomingRequests, useOutgoingRequests } from "@/features/social/model/queries";
import { RelationshipActions } from "@/features/social/ui/relationship-actions";
import { useTranslation } from "@/shared/i18n";
import { formatDate } from "@/shared/lib/format";
import { ListState } from "@/shared/ui";
import { Button, Spinner } from "@/shared/ui";
import { PlayerRow } from "@/widgets/player-row";
import { SocialNav } from "@/widgets/social-nav";

/**
 * `/friends/requests` — both directions, one page.
 *
 * ## Why one page and not two routes
 *
 * They are two views of one resource and a person checking requests wants
 * both. Two routes would mean a badge on each and a decision about which to
 * land on; two sections answer the same need with one navigation step.
 *
 * ## Cancel is not decline
 *
 * An outgoing request is **withdrawn** by its sender; an incoming one is
 * **declined** by its recipient. The API has two endpoints and the two
 * words mean different things to the person reading them, so the labels
 * differ and `RelationshipActions` derives which is offered from the state
 * the server sent — `outgoing_request` versus `incoming_request` — rather
 * than from which list rendered it.
 *
 * That is why the request lists state their relationship server-side: the
 * direction *is* the state, and a client that guessed would offer "accept"
 * on a request it sent.
 *
 * ## No cooldown UI
 *
 * The API publishes no cooldown for friend requests, so nothing here
 * invents one. A repeated cancel of an already-resolved request is a
 * bounded `404`/`409`, which the shared error mapping renders.
 */
export default function FriendRequestsPage() {
  const { t, locale } = useTranslation();
  const incoming = useIncomingRequests();
  const outgoing = useOutgoingRequests();

  const incomingItems = incoming.data?.pages.flatMap((page) => page.items) ?? [];
  const outgoingItems = outgoing.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <SocialNav title={t("social.requests.title")}>
      <div className="flex flex-col gap-10">
        <section aria-labelledby="incoming-heading" className="flex flex-col gap-4">
          <h2 id="incoming-heading" className="text-base font-semibold">
            {t("social.requests.incoming")}
          </h2>

          <ListState
            isPending={incoming.isPending}
            isError={incoming.isError}
            isEmpty={incomingItems.length === 0}
            emptyTitle={t("social.requests.incomingEmpty")}
            onRetry={() => void incoming.refetch()}
          >
            <ul
              aria-label={t("social.requests.incoming")}
              className="border-border bg-card divide-border flex flex-col divide-y overflow-hidden rounded-xl border"
            >
              {incomingItems.map((request) => (
                <PlayerRow
                  key={request.id}
                  player={request.player}
                  meta={t("social.requests.sent", {
                    date: formatDate(request.created_at, locale) ?? "",
                  })}
                  actions={
                    <RelationshipActions
                      playerId={request.player.id}
                      playerName={request.player.display_name ?? request.player.username}
                      state={request.player.relationship}
                      requestId={request.id}
                    />
                  }
                />
              ))}
            </ul>
          </ListState>

          {incoming.hasNextPage && (
            <Button
              variant="outline"
              className="min-h-11 self-start"
              disabled={incoming.isFetchingNextPage}
              onClick={() => void incoming.fetchNextPage()}
            >
              {incoming.isFetchingNextPage ? (
                <Spinner label={t("state.loading")} />
              ) : (
                t("social.requests.loadMore")
              )}
            </Button>
          )}
        </section>

        <section aria-labelledby="outgoing-heading" className="flex flex-col gap-4">
          <h2 id="outgoing-heading" className="text-base font-semibold">
            {t("social.requests.outgoing")}
          </h2>

          <ListState
            isPending={outgoing.isPending}
            isError={outgoing.isError}
            isEmpty={outgoingItems.length === 0}
            emptyTitle={t("social.requests.outgoingEmpty")}
            onRetry={() => void outgoing.refetch()}
          >
            <ul
              aria-label={t("social.requests.outgoing")}
              className="border-border bg-card divide-border flex flex-col divide-y overflow-hidden rounded-xl border"
            >
              {outgoingItems.map((request) => (
                <PlayerRow
                  key={request.id}
                  player={request.player}
                  meta={t("social.requests.sent", {
                    date: formatDate(request.created_at, locale) ?? "",
                  })}
                  actions={
                    <RelationshipActions
                      playerId={request.player.id}
                      playerName={request.player.display_name ?? request.player.username}
                      state={request.player.relationship}
                      requestId={request.id}
                    />
                  }
                />
              ))}
            </ul>
          </ListState>

          {outgoing.hasNextPage && (
            <Button
              variant="outline"
              className="min-h-11 self-start"
              disabled={outgoing.isFetchingNextPage}
              onClick={() => void outgoing.fetchNextPage()}
            >
              {outgoing.isFetchingNextPage ? (
                <Spinner label={t("state.loading")} />
              ) : (
                t("social.requests.loadMore")
              )}
            </Button>
          )}
        </section>
      </div>
    </SocialNav>
  );
}
