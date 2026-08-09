import { Link, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useRef } from "react";

import { matchOf } from "@/entities/queue";
import { useLobbyState } from "@/features/matchmaking/model/lobby-state";
import { QueueForm } from "@/features/matchmaking/ui/queue-form";
import { WaitingCard } from "@/features/matchmaking/ui/waiting-card";
import { useTranslation } from "@/shared/i18n";
import { useHoldAppUpdate } from "@/shared/pwa";
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
 * **A64-022.6 §13 moved the dialog itself to `AppShell`**, which changes
 * nothing about that: it still renders over this page and this page still
 * shows the queue behind it. What changed is that it now also renders over
 * every other authenticated page, which is where a challenge acceptance
 * needed it.
 *
 * ## Why this page still navigates, when the shell also does — A64-022.7 §11
 *
 * Two owners, and they are kept because they fire on **different triggers**
 * rather than because they happen to agree on a URL:
 *
 *     MatchOfferSurface   "an offer I showed became active" — the handoff,
 *                         wherever the player is
 *     PlayPage            "the lobby's derived state is transitioning" —
 *                         which is true on a **reload** of this page with a
 *                         match already active
 *
 * The second case is the one the shell deliberately declines, and it must:
 * `pending_for` reports an active match with no time window, so a shell
 * that navigated on it would drag a player out of `/profile` for the whole
 * duration of a game. Here it is correct — reloading the lobby mid-handoff
 * should land on the board, which is this page's documented recovery.
 *
 * Unifying them would mean the shell checking which route it is on, which
 * is worse than two guarded effects that target the same place.
 *
 * ## Navigation happens once, and in an effect
 *
 * Two rules, and the second is the one that bites. `navigated` guards
 * against a double handoff — both the accept response and the derived
 * `transitioning` state can produce one, and a route change dispatched
 * twice is a lost history entry at best.
 *
 * A boolean is right **here** and was wrong in the shell: this page unmounts
 * on the way to the board and remounts with a fresh ref, where `AppShell`
 * never unmounts — see `MatchOfferSurface.handedOff`.
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

  // A64-020.9 §14. An acceptance window is seconds long and a reload
  // inside it loses the offer — the countdown expires while the page is
  // reassembling itself, and the player is told their opponent left.
  // `transitioning` is held for the same reason: the navigation to the
  // board has already been decided and is in flight.
  useHoldAppUpdate(
    lobby.state.status === "match_offer" ||
      lobby.state.status === "awaiting_opponent" ||
      lobby.state.status === "transitioning",
  );

  return (
    // `tabIndex={-1}` so focus has somewhere sensible to land when the
    // offer dialog closes — §23. Without it Radix returns focus to `body`
    // and a keyboard user restarts from the top of the document.
    <section tabIndex={-1} className="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold tracking-tight">{t("play.title")}</h1>
        <p className="text-muted-foreground text-sm">{t("play.subtitle")}</p>
      </div>

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
        // The setup sits on its own surface so the sticky action bar has
        // something to sit *on*, and so the page reads as one object rather
        // than two fieldsets floating on the background.
        <div className="border-border bg-card rounded-xl border p-4 shadow-sm sm:p-6">
          <QueueForm disabled={match !== null} />
        </div>
      )}

      {/* A64-025.5 §14. The second way to start a game, named and linked
          rather than rebuilt. Challenge creation lives at `/challenges` and
          this is a deep link to it — a lobby that reimplemented the flow
          would be a second implementation of a shared surface, which §23
          rules out. Below the queue, because a random opponent is the
          faster path and the one this page is for. */}
      {lobby.state.status !== "queued" && lobby.state.status !== "unavailable" && (
        <div className="border-border flex flex-col gap-3 rounded-xl border border-dashed p-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-col gap-0.5">
            <p className="text-sm font-medium">{t("play.friend.friendTitle")}</p>
            <p className="text-muted-foreground text-sm">{t("play.friend.friendBody")}</p>
          </div>
          <Button asChild variant="outline" className="w-full sm:w-auto">
            <Link to="/challenges">{t("play.friend.friendCta")}</Link>
          </Button>
        </div>
      )}

      {/* The offer dialog is **not** rendered here — A64-022.6 §13.
          `AppShell` owns the one instance in the app, so a player paired
          while reading a profile sees it too. This page still derives
          `match` for its own state: the form is disabled while an offer is
          open, and the transitioning navigation below is this page's
          documented reload recovery. */}
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
