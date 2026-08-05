import { useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useRef } from "react";

import { matchOf } from "@/entities/queue";
import { useLobbyState } from "@/features/matchmaking/model/lobby-state";
import { MatchOfferDialog } from "@/features/matchmaking/ui/match-offer-dialog";
import { QueueForm } from "@/features/matchmaking/ui/queue-form";
import { WaitingCard } from "@/features/matchmaking/ui/waiting-card";
import { useTranslation } from "@/shared/i18n";
import { Button, Skeleton } from "@/shared/ui";

/**
 * The lobby — A64-020.5A §2, §9, §19.
 *
 * One route, one derived state, three renderings. The page itself decides
 * almost nothing: `useLobbyState` computes what is happening from the two
 * authoritative reads, and this switches on it.
 *
 * ## Recovery is the default path, not a special case
 *
 * There is no "restore" branch here, because there is nothing to restore.
 * A reload runs exactly the same two queries a first visit does, and both
 * answer from the server — so a refreshed page reconstructs the chosen
 * mode, the chosen clock, the instant the player queued and any open offer
 * without a line of code that knows it is a reload. §9 forbids
 * `localStorage` for this state and nothing here would have a use for it.
 *
 * ## The offer is rendered *beside* the lobby, not instead of it
 *
 * The dialog is modal and interrupting; behind it the page keeps showing
 * whatever the queue says. That is deliberate: when the offer resolves —
 * declined, expired, or the opponent vanished — the player is left looking
 * at the real state rather than at a blank page waiting for a refetch.
 *
 * ## Navigation happens once, and in an effect
 *
 * Two rules, and the second is the one that bites. `navigated` guards
 * against a double handoff — both the accept response and the derived
 * `transitioning` state can produce one, and a route change dispatched
 * twice is a lost history entry at best.
 *
 * And it runs in an effect rather than during render. Navigating while
 * rendering asks the router to change state in the middle of React's
 * commit, which re-renders this component, which navigates again: the
 * infinite-update loop A64-020.2 already found once in `RequireAuth`. The
 * ref alone does not prevent it, because the first call happens before the
 * ref is set on the *next* mount.
 */
export default function PlayPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const lobby = useLobbyState();
  const navigated = useRef(false);

  const goToGame = useCallback(
    (matchId: string) => {
      if (navigated.current) return;
      navigated.current = true;
      void navigate({ to: "/games/$matchId", params: { matchId } });
    },
    [navigate],
  );

  const readyMatchId = lobby.state.status === "transitioning" ? lobby.state.matchId : null;
  useEffect(() => {
    if (readyMatchId !== null) goToGame(readyMatchId);
  }, [readyMatchId, goToGame]);

  const match = matchOf(lobby.state);

  return (
    // `tabIndex={-1}` so focus has somewhere sensible to land when the
    // offer dialog closes — §23. Without it Radix returns focus to `body`
    // and a keyboard user restarts from the top of the document.
    <section tabIndex={-1} className="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <h1 className="text-2xl font-semibold tracking-tight">{t("play.title")}</h1>

      {lobby.state.status === "unavailable" ? (
        <Unavailable onRetry={lobby.refetch} />
      ) : lobby.isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : lobby.state.status === "queued" ? (
        <WaitingCard ticket={lobby.state.ticket} />
      ) : (
        // Also the `match_offer` and `awaiting_opponent` case: the form is
        // **disabled**, not hidden, so the page behind the dialog does not
        // reflow the moment an offer arrives — and §6's "hidden or disabled
        // while the player has a live ticket or pending match" is satisfied
        // by a control that cannot be submitted.
        <QueueForm disabled={match !== null} />
      )}

      {match !== null && (
        <MatchOfferDialog
          // Keyed by the match, so a second offer after a decline mounts a
          // fresh dialog rather than reusing one whose countdown is running
          // against the previous deadline.
          key={match.match_id}
          match={match}
          onExpired={lobby.refetch}
          onAccepted={goToGame}
        />
      )}
    </section>
  );
}

function Unavailable({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <div role="alert" className="flex flex-col items-start gap-3">
      <p className="text-sm">{t("play.errors.unavailable")}</p>
      <Button variant="outline" className="min-h-11" onClick={onRetry}>
        {t("common.retry")}
      </Button>
    </div>
  );
}
