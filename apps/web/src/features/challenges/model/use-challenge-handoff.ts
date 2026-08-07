import { useNavigate } from "@tanstack/react-router";
import { useCallback, useRef } from "react";

import { useAcceptMatch } from "@/features/matchmaking/model/queries";

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
 * for the UI to close **in UX, not in architecture**.
 *
 * So this closes it by chaining two existing calls into one press:
 *
 *     POST /challenges/{id}/accept        the challenge is answered and the
 *                                         match is created, atomically
 *     POST /matchmaking/matches/{id}/accept   this player takes their seat
 *
 * If the challenger has already taken theirs, the second call comes back
 * `active` and the navigation happens immediately — one press, and the game
 * opens.
 *
 * ## What this no longer does — A64-022.6 §13
 *
 * It used to own the *waiting* half too: a pending-match query, the offer
 * push subscription, `derive`, and a `MatchOfferDialog` rendered on the
 * challenge page. All four are gone, because `AppShell` now mounts
 * `MatchOfferSurface` on every authenticated page.
 *
 * That is a strict improvement rather than a relocation. The old
 * arrangement worked only while the recipient stayed on `/challenges`; the
 * new one follows them anywhere, and it removed the second place a pending
 * match was watched. What is left here is the chain and the navigation for
 * the case that resolves immediately — which is this feature's own concern
 * and nothing else's.
 *
 * ## Navigation happens once, and never during render
 *
 * `PlayPage`'s rules, for its reasons: a ref guards a double dispatch, and
 * nothing navigates inside React's commit.
 */
export interface ChallengeHandoff {
  /**
   * Take the seat in a match this player just created by accepting.
   *
   * Navigates when the opponent is already in. Otherwise returns, and the
   * shell's offer surface takes over — it is already watching the same
   * pending read, which `useAcceptChallenge` invalidated.
   */
  join: (matchId: string) => Promise<void>;
  /** Go to the board. Idempotent — a second call after the first is a no-op. */
  goToGame: (matchId: string) => void;
}

export function useChallengeHandoff(): ChallengeHandoff {
  const navigate = useNavigate();
  const acceptMatch = useAcceptMatch();

  const navigated = useRef(false);
  const goToGame = useCallback(
    (matchId: string) => {
      if (navigated.current) return;
      navigated.current = true;
      void navigate({ to: "/games/$matchId", params: { matchId } });
    },
    [navigate],
  );

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

  return { join, goToGame };
}
