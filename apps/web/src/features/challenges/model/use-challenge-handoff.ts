import { useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useRef } from "react";

import { type LobbyState, matchOf, type PendingMatch } from "@/entities/queue";
import { isResolved } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { derive } from "@/features/matchmaking/model/lobby-state";
import { useAcceptMatch, usePendingMatch } from "@/features/matchmaking/model/queries";
import { useMatchOfferPush } from "@/features/matchmaking/model/use-match-offers";

/**
 * Accept a challenge, and end up at the board — A64-022.5 §6, §7.
 *
 * ## The problem this exists to hide
 *
 * A64-022.3 makes acceptance create a **`BILATERAL`** match: the game
 * exists the moment the recipient says yes, and both players must still
 * join it inside a ten-minute window. That is the right backend shape —
 * `MatchRecord` refuses a system-activated timed match, because nothing
 * would schedule its flag deadline — and A64-022.4 recorded it as a seam
 * for this phase to close **in UX, not in architecture**.
 *
 * So this closes it by chaining two existing calls into one press:
 *
 *     POST /challenges/{id}/accept        the challenge is answered and the
 *                                         match is created, atomically
 *     POST /matchmaking/matches/{id}/accept   this player takes their seat
 *
 * If the challenger has already taken theirs, the second call comes back
 * `active` and the navigation happens immediately — one press, and the game
 * opens. If they have not, the ordinary match offer surface takes over and
 * says so.
 *
 * ## Nothing new watches for the opponent
 *
 * The waiting half is `matchmaking`'s, reused whole: `usePendingMatch` keeps
 * its two-second interval **while an offer is open regardless of a queue
 * ticket**, which is precisely the case here — a challenge match has no
 * ticket. `useMatchOfferPush` supplies the same wake-up frame the lobby
 * gets, and `derive` supplies the same precedence.
 *
 * That reuse is the point rather than a convenience. A second definition of
 * "is this game ready" would be a second thing to get wrong about a
 * ten-minute window, and §20's "no polling" means *this feature adds none* —
 * not that the platform's existing match reconciliation is switched off.
 *
 * `ticket: null` is passed to `derive` because there is no queue ticket to
 * consider on this page. Every match-driven state it produces —
 * `match_offer`, `awaiting_opponent`, `transitioning` — is exactly what the
 * lobby would produce for the same match.
 *
 * ## Navigation happens once, and in an effect
 *
 * `PlayPage`'s two rules, and they apply here for the same reasons: the
 * accept response and the derived `transitioning` state can each produce a
 * handoff, so a ref guards against dispatching two; and navigating during
 * render asks the router to change state inside React's commit, which
 * re-renders this hook's owner, which navigates again.
 */
export interface ChallengeHandoff {
  /** The offer to render, or `null`. Drives the shared `MatchOfferDialog`. */
  match: PendingMatch | null;
  /** The derived match state, for a page that wants to say what is happening. */
  state: LobbyState;
  /**
   * Take the seat in a match this player just created by accepting.
   *
   * Navigates when the opponent is already in. Otherwise returns, and the
   * offer surface above takes over.
   */
  join: (matchId: string) => Promise<void>;
  /** Go to the board. Idempotent — a second call after the first is a no-op. */
  goToGame: (matchId: string) => void;
  /** Re-ask the pending read. The recovery a lapsed countdown offers. */
  refetch: () => void;
}

export function useChallengeHandoff(): ChallengeHandoff {
  const navigate = useNavigate();
  const { state: session } = useSession();
  const acceptMatch = useAcceptMatch();

  // The same wake-up subscription the lobby mounts. A challenge match is
  // created by the accept transaction, so `matchmaking.match.offered`
  // reaches both players through the path A64-022.4's audit found already
  // in place — no origin filter, no new frame.
  useMatchOfferPush();

  const pending = usePendingMatch(false);

  const navigated = useRef(false);
  const goToGame = useCallback(
    (matchId: string) => {
      if (navigated.current) return;
      navigated.current = true;
      void navigate({ to: "/games/$matchId", params: { matchId } });
    },
    [navigate],
  );

  const state = derive({
    session: isResolved(session),
    // No ticket to consider: this page is not the lobby, and a challenge
    // match is not reached through a queue.
    ticket: null,
    match: pending.data,
  });

  const readyMatchId = state.status === "transitioning" ? state.matchId : null;
  useEffect(() => {
    if (readyMatchId !== null) goToGame(readyMatchId);
  }, [readyMatchId, goToGame]);

  const join = useCallback(
    async (matchId: string) => {
      // The server's own word for "both of you agreed". Deriving it from
      // `you_accepted && opponent_accepted` would be a second definition of
      // activation, and this one is the record's.
      const answered = await acceptMatch.mutateAsync(matchId);
      if (answered.status === "active") goToGame(answered.match_id);
    },
    [acceptMatch, goToGame],
  );

  return {
    match: matchOf(state),
    state,
    join,
    goToGame,
    refetch: () => void pending.refetch(),
  };
}
