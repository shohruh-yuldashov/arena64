import { useState } from "react";

import type { Challenge } from "@/features/challenges/api";
import {
  useAcceptChallenge,
  useCancelChallenge,
  useDeclineChallenge,
  useIncomingChallenges,
  useOutgoingChallenges,
} from "@/features/challenges/model/queries";
import { useChallengeHandoff } from "@/features/challenges/model/use-challenge-handoff";
import { useChallengePush } from "@/features/challenges/model/use-challenge-push";
import { useTimeControls } from "@/features/matchmaking/model/queries";
import { MatchOfferDialog } from "@/features/matchmaking/ui/match-offer-dialog";
import { ListState } from "@/features/social/ui/list-state";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { useHoldAppUpdate } from "@/shared/pwa";
import { Button, Spinner } from "@/shared/ui";
import { ChallengeRow } from "@/widgets/challenge-row";
import { SocialNav } from "@/widgets/social-nav";

/**
 * `/challenges` — invitations, both directions — A64-022.5 §3, §6, §7.
 *
 * ## Two tabs, two endpoints, no client-side split
 *
 * Incoming and outgoing are separate reads with separate actions, and the
 * page renders whichever the tab selects. Nothing here decides which side
 * of a challenge the viewer is on — the server already did, by answering
 * two different questions.
 *
 * Tabs rather than two stacked lists: on a 360px screen two lists is a page
 * where the second one is below the fold and nobody finds it, and the
 * common case is looking at one of them.
 *
 * ## Accept is one press, and this is where that is arranged
 *
 * §6, §7. A64-022.3 makes acceptance create a `BILATERAL` match — the game
 * exists and both players must still take their seats. So Accept chains the
 * challenge accept into `matchmaking`'s match accept, and if the challenger
 * is already in, the navigation happens without a second interaction.
 *
 * If they are not, the **shared** `MatchOfferDialog` takes over: the same
 * component the lobby renders, driven by the same query, saying "waiting
 * for your opponent" with the same countdown. See `useChallengeHandoff` for
 * why none of that is re-implemented here.
 *
 * ## Nothing polls the lists — §20
 *
 * `useChallengePush` invalidates both on the `notification.created` frame
 * for either challenge type, and the read decides the rest. The one thing
 * watched on this page is the pending match, by `matchmaking`'s own query
 * with `matchmaking`'s own interval — which is what it has always done for
 * an open offer, ticket or no ticket.
 */
type Tab = "incoming" | "outgoing";

export default function ChallengesPage() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("incoming");

  // Both mounted, so switching tabs is instant after the first visit and so
  // a realtime invalidation refreshes whichever is not on screen too. Two
  // cursor pages is a bounded cost; a refetch on every tab press is not.
  const incoming = useIncomingChallenges();
  const outgoing = useOutgoingChallenges();
  const controls = useTimeControls();

  useChallengePush();
  const handoff = useChallengeHandoff();

  const accept = useAcceptChallenge();
  const decline = useDeclineChallenge();
  const cancel = useCancelChallenge();

  // A64-020.9 §14. An acceptance window is seconds long and a reload inside
  // it loses the offer, so the update prompt waits — the same hold the
  // lobby applies, for the same offer.
  useHoldAppUpdate(handoff.match !== null || handoff.state.status === "transitioning");

  const list = tab === "incoming" ? incoming : outgoing;
  const items: Challenge[] = list.data?.pages.flatMap((page) => page.items) ?? [];

  /** Accept, then take the seat. One press — see this module's docstring. */
  const onAccept = async (challengeId: string) => {
    const answered = await accept.mutateAsync(challengeId);
    if (answered.created_match_id !== null && answered.created_match_id !== undefined) {
      await handoff.join(answered.created_match_id);
    }
  };

  return (
    <SocialNav title={t("challenges.title")} description={t("challenges.description")}>
      <div className="flex flex-col gap-4">
        {/* A tab list, with the roles that make arrow-key navigation and
            "tab 1 of 2" announcements work. The panel is labelled by its
            tab, so a screen reader user knows which list they are in. */}
        <div role="tablist" aria-label={t("challenges.title")} className="flex gap-2">
          {(["incoming", "outgoing"] as const).map((value) => (
            <button
              key={value}
              type="button"
              role="tab"
              id={`challenges-tab-${value}`}
              aria-selected={tab === value}
              aria-controls={`challenges-panel-${value}`}
              onClick={() => setTab(value)}
              className={cn(
                "focus-visible:ring-ring min-h-11 rounded-md px-3 text-sm focus-visible:ring-2 focus-visible:outline-none",
                tab === value
                  ? "bg-muted text-foreground font-medium"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {t(
                value === "incoming" ? "challenges.tabs.incoming" : "challenges.tabs.outgoing",
              )}
            </button>
          ))}
        </div>

        <div
          role="tabpanel"
          id={`challenges-panel-${tab}`}
          aria-labelledby={`challenges-tab-${tab}`}
          tabIndex={-1}
          className="flex flex-col gap-4"
        >
          <ListState
            isPending={list.isPending}
            isError={list.isError}
            isEmpty={items.length === 0}
            emptyTitle={t(
              tab === "incoming"
                ? "challenges.empty.incomingTitle"
                : "challenges.empty.outgoingTitle",
            )}
            emptyHint={t(
              tab === "incoming"
                ? "challenges.empty.incomingHint"
                : "challenges.empty.outgoingHint",
            )}
            onRetry={() => void list.refetch()}
          >
            <ul
              aria-label={t(
                tab === "incoming" ? "challenges.tabs.incoming" : "challenges.tabs.outgoing",
              )}
              className="flex flex-col gap-2"
            >
              {items.map((challenge) => (
                <ChallengeRow
                  key={challenge.id}
                  challenge={challenge}
                  controls={controls.data}
                  actions={
                    tab === "incoming"
                      ? {
                          kind: "incoming",
                          onAccept,
                          onDecline: (id) => decline.mutateAsync(id),
                        }
                      : { kind: "outgoing", onCancel: (id) => cancel.mutateAsync(id) }
                  }
                />
              ))}
            </ul>
          </ListState>

          {list.hasNextPage && (
            <Button
              variant="outline"
              className="min-h-11 self-start"
              disabled={list.isFetchingNextPage}
              onClick={() => void list.fetchNextPage()}
            >
              {list.isFetchingNextPage ? (
                <Spinner label={t("social.state.loading")} />
              ) : (
                t("challenges.loadMore")
              )}
            </Button>
          )}
        </div>
      </div>

      {handoff.match !== null && (
        <MatchOfferDialog
          // Keyed by the match, so a second offer mounts a fresh dialog
          // rather than reusing one whose countdown runs against the
          // previous deadline.
          key={handoff.match.match_id}
          match={handoff.match}
          onExpired={handoff.refetch}
          onAccepted={handoff.goToGame}
        />
      )}
    </SocialNav>
  );
}
