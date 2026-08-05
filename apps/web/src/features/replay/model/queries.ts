import { useQuery } from "@tanstack/react-query";

import { isResolved } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { readReplay } from "@/features/replay/api";
import { replayKeys } from "@/features/replay/api/keys";
import { ApiError } from "@/shared/api";

/**
 * The replay read — A64-020.5E §4, §16, §17.
 *
 * ## Immutable, so it is cached for the session
 *
 * A finished match's log does not change and neither does the engine
 * version that would refuse it, so `staleTime: Infinity` is the honest
 * answer rather than a long guess. Nothing on this platform can invalidate
 * it: there is no mutation that touches a completed game.
 *
 * The consequence worth stating is what it buys — stepping through a
 * hundred plies is **zero** requests, because every position is already in
 * the one response (§23).
 *
 * ## Errors are not retried, and the reason is which errors these are
 *
 * `404` means the match does not exist *or* the viewer may not see it, and
 * `409` means the engine version is unsupported. Both are stable answers
 * about a permanent record: retrying either would be asking the same
 * question of the same immutable row. Only an unexpected failure is worth
 * a second attempt, and the shared default already handles that.
 */
export function useReplay(matchId: string) {
  const { state } = useSession();
  return useQuery({
    queryKey: replayKeys.byMatch(matchId),
    queryFn: () => readReplay(matchId),
    enabled: isResolved(state),
    staleTime: Infinity,
    gcTime: Infinity,
    // Never refetched on focus. A permanent record cannot have changed
    // since the tab lost focus, and a refetch would be a request that can
    // only return what is already held.
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && (error.status === 404 || error.status === 409)) {
        return false;
      }
      return failureCount < 2;
    },
  });
}

/** What kind of refusal this is, for the screen that renders it. */
export type ReplayRefusal = "not_found" | "unsupported_engine_version" | "unexpected";

/**
 * One error as a bounded state — §16, §17.
 *
 * `unsupported_engine_version` is a **first-class state, not an error**
 * (§16): the match exists, the viewer may see it, and this build declines
 * to reconstruct it rather than approximating under rules that have since
 * been fixed.
 *
 * `404` covers a match that does not exist and a casual match the viewer
 * did not play, and the two are **indistinguishable by design** — so they
 * resolve to one state here, and the screen must not say "you do not have
 * permission" (§17).
 */
export function refusalOf(error: unknown): ReplayRefusal {
  if (!(error instanceof ApiError)) return "unexpected";
  if (error.status === 404) return "not_found";
  if (error.code === "unsupported_engine_version") return "unsupported_engine_version";
  return "unexpected";
}
