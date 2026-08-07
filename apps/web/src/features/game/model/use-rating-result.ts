import { useQuery, useQueryClient } from "@tanstack/react-query";

import type { MatchHistoryEntry, MatchHistoryPage } from "@/features/match-history/api";
import { readMatchHistory } from "@/features/match-history/api";
import { matchHistoryKeys } from "@/features/match-history/api/keys";

/**
 * What the match just played did to this player's rating — A64-023 §6.
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
 * ## Bounded, and it stops for the right reason
 *
 * The projection normally lands within a relay tick. This asks again a
 * small number of times and then stops:
 *
 *     rating present   stop — `refetchInterval` returns false
 *     casual match     never starts; the caller does not enable it
 *     still absent     stop after `MAX_ATTEMPTS`, and say so
 *
 * `retry: false` on top, because a *failed* request is a different thing
 * from a pending projection and must not be retried into the attempt
 * budget. There is no path here that polls forever, and none that renders a
 * fabricated zero while it waits.
 *
 * ## One row, read through the existing page
 *
 * `limit: 1` on the player's own history: the match that just ended is the
 * newest finished one, so the first row is it. Reusing the history endpoint
 * is what keeps this from being a second result contract — see
 * `specs/frontend.md`.
 */

/** How long between attempts. Short: the relay tick is the thing being waited on. */
export const RATING_POLL_MS = 1_500;

/** How many attempts before the UI stops asking and says so. */
export const RATING_MAX_ATTEMPTS = 5;

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
  const active = enabled && viewerId !== null;
  const key = [...matchHistoryKeys.player(viewerId ?? "anonymous"), "rating", matchId];

  const query = useQuery({
    queryKey: key,
    queryFn: () => readMatchHistory(viewerId as string, { limit: 1 }),
    enabled: active,
    retry: false,
    // **No `gcTime: 0`.** The attempt count lives on the cache entry, and
    // collecting the entry between renders resets it — which would make the
    // budget below unreachable and turn a bounded reconciliation into an
    // unbounded one. Found by the test that asserts it stops.
    refetchInterval: (q) => {
      const row = rowIn(q.state.data, matchId);
      // A casual match has nothing to wait for, and the row is where that
      // is stated — one request, then silence.
      if (row !== undefined && !row.rated) return false;
      if (row?.rating != null) return false;
      // `dataUpdateCount` counts answered fetches, so this is "how many
      // times have we asked", not "how long have we waited" — a slow
      // network spends the budget on answers rather than on timeouts.
      return q.state.dataUpdateCount >= RATING_MAX_ATTEMPTS ? false : RATING_POLL_MS;
    },
  });

  // The attempt count lives on the cache entry rather than on the
  // observer, and it is the **same number** `refetchInterval` above stops
  // on — so what the copy says and what the schedule does cannot disagree.
  const state = useQueryClient().getQueryState<MatchHistoryPage>(key);

  const row = rowIn(query.data, matchId);
  const change = row?.rating ?? null;
  const rated = row === undefined ? null : row.rated;
  const attempts = state?.dataUpdateCount ?? 0;
  const waiting = active && rated !== false && change === null;

  return {
    change,
    rated,
    isPending: waiting && attempts < RATING_MAX_ATTEMPTS,
    hasGivenUp: waiting && attempts >= RATING_MAX_ATTEMPTS,
  };
}

/**
 * The row for this match, or `undefined` if the page has not answered yet.
 *
 * Matched by id rather than taken from position 0: the newest finished
 * match is this one in every ordinary case, and reading position 0 blindly
 * would show the *previous* game's delta in the one case it is not.
 */
function rowIn(
  page: MatchHistoryPage | undefined,
  matchId: string,
): MatchHistoryEntry | undefined {
  return page?.entries.find((entry) => entry.match_id === matchId);
}
