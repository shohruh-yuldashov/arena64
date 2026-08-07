import { useEffect, useRef, useState } from "react";

import type { MatchHistoryEntry } from "@/features/match-history/api";
import { readMatchHistory } from "@/features/match-history/api";

/**
 * What the match just played did to this player's rating — A64-024.
 *
 * ## Why this reads history rather than the completion frame
 *
 * `MatchRatingService` consumes `game.match_completed` through the outbox,
 * so the adjustment is written *after* the match ends. Putting a delta on
 * `game.completed` would put a field on the wire that is sometimes there
 * and sometimes not, for reasons a client could not distinguish — so the
 * authority is the read model, which reports honestly that it is not ready
 * yet.
 *
 * ## Why this is a timeout loop and not `refetchInterval`
 *
 * The first version used TanStack's `refetchInterval`, and it stuck on
 * "Rating is being updated…" **forever** in a real browser. That scheduler
 * is entangled with things this reconciliation has no business depending
 * on: document visibility, observer mount/unmount, the cache entry's
 * lifetime, and an attempt counter that lives on the cache entry — so a
 * collected entry silently reset the budget and the bound became
 * unreachable.
 *
 * Worse, it was untestable: `refetchInterval` does not run while the
 * document is hidden and jsdom never reports it visible, so the bound could
 * not be asserted at all. A mechanism whose failure mode is "waits forever"
 * and which cannot be tested is the wrong mechanism.
 *
 * This is a plain effect with a `setTimeout` chain. It has one property the
 * other lacked: **everything it depends on is in this file.** The attempt
 * count is a local, the loop is cancelled by the effect's own cleanup, and
 * the identity of the reconciliation is `(matchId, viewerId)` — so a
 * re-render cannot start a second timer and unmounting stops the only one.
 *
 * ## Bounded, and it stops for the right reason
 *
 *     rating present   stop, and render it
 *     casual match     stop after the first answer — the row says `rated`
 *     request failed   counts as an attempt; no separate retry budget
 *     still absent     stop after MAX_ATTEMPTS and say so
 *
 * There is no path here that polls forever.
 */

/** How long between attempts. The relay tick is the thing being waited on. */
export const RATING_POLL_MS = 1_500;

/** How many attempts before the UI stops asking and says so. */
export const RATING_MAX_ATTEMPTS = 6;

export interface RatingResult {
  /** The authoritative change, once the projection has landed. */
  change: { before: number; after: number; delta: number } | null;
  /**
   * Whether the match was rated at all — read from the row, because the
   * live game state does not carry it. `null` until the first answer.
   */
  rated: boolean | null;
  /** Still waiting, and still within the attempt budget. */
  isPending: boolean;
  /** The budget ran out. The result stays on screen; this only changes the copy. */
  hasGivenUp: boolean;
}

type Outcome = {
  change: RatingResult["change"];
  rated: boolean | null;
  settled: boolean;
};

const WAITING: Outcome = { change: null, rated: null, settled: false };

export function useRatingResult({
  matchId,
  viewerId,
  enabled,
}: {
  matchId: string;
  viewerId: string | null;
  /** The game has actually finished. */
  enabled: boolean;
}): RatingResult {
  const [outcome, setOutcome] = useState<Outcome>(WAITING);

  // Reset when the reconciliation's identity changes, so a second game in
  // one session does not inherit the first one's answer for a frame.
  const identity = `${matchId}:${viewerId ?? ""}`;
  const lastIdentity = useRef(identity);
  if (lastIdentity.current !== identity) {
    lastIdentity.current = identity;
    if (outcome !== WAITING) setOutcome(WAITING);
  }

  useEffect(() => {
    if (!enabled || viewerId === null) return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    let attempts = 0;

    const attempt = async () => {
      attempts += 1;
      let row: MatchHistoryEntry | undefined;
      try {
        // The viewer's own newest finished match. The rating block is
        // served only for your own history, which is what makes this the
        // authoritative read for it — see the backend route.
        const page = await readMatchHistory(viewerId, { limit: 1 });
        row = page.entries.find((entry) => entry.match_id === matchId);
      } catch {
        // A failed request spends an attempt rather than starting a second
        // budget: the caller's question is "is it ready", and a network
        // error is one more "not yet" with the same bound.
        row = undefined;
      }
      if (cancelled) return;

      const change = row?.rating ?? null;
      const rated = row === undefined ? null : row.rated;

      // Three ways to be finished, and all of them stop the loop.
      const done = change !== null || rated === false || attempts >= RATING_MAX_ATTEMPTS;
      setOutcome({ change, rated, settled: done });
      if (done) return;

      timer = setTimeout(() => void attempt(), RATING_POLL_MS);
    };

    void attempt();

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [enabled, matchId, viewerId]);

  const active = enabled && viewerId !== null;
  const waiting = active && outcome.rated !== false && outcome.change === null;

  return {
    change: outcome.change,
    rated: outcome.rated,
    isPending: waiting && !outcome.settled,
    hasGivenUp: waiting && outcome.settled,
  };
}
