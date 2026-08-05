import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";

import { ApiError, normalizeError } from "@/shared/api/errors";
import { reportError } from "@/shared/lib/report-error";

/**
 * The cache policy, and why each number is the number it is.
 *
 * Every value below is argued from what Arena64's data actually does, not
 * copied from a starter template. Where a query's own volatility differs —
 * a live clock, a bracket mid-round — it overrides these at the call site;
 * these are the defaults for data with no stated opinion.
 *
 * | Option                 | Value  | Why                                      |
 * | ---------------------- | ------ | ---------------------------------------- |
 * | `staleTime`            | 30 s   | see below                                |
 * | `gcTime`               | 15 min | see below                                |
 * | `retry`                | typed  | see below                                |
 * | `refetchOnWindowFocus` | `true` | see below                                |
 *
 * **`staleTime: 30_000`.** Zero — TanStack's default — means every mount
 * refetches, so navigating away and back re-requests a leaderboard that
 * cannot have moved. Arena64's reads are ladders, brackets and profiles:
 * they change on the scale of a finished game, not a keystroke. Half a
 * minute is short enough that a rating update is never stale on screen for
 * long and long enough that ordinary navigation is free. The truly live
 * surfaces — a game in progress, a queue — do not poll at all; they arrive
 * over the WebSocket (AD-11), which is why this number does not have to
 * compromise for them.
 *
 * **`gcTime: 900_000`.** How long an *unused* cache entry survives before
 * it is discarded, so it must be comfortably longer than `staleTime` or
 * a back-navigation finds nothing and renders a spinner instead of stale
 * content it could revalidate. Fifteen minutes is roughly a session's
 * attention span; the cost of holding a few JSON pages that long is
 * kilobytes.
 *
 * **`retry`.** Never a bare number. A 404, a 422 and a 401 will fail
 * identically however many times they are sent, and retrying them turns
 * one user-visible failure into three and one server log line into three
 * (CLAUDE.md §9.10). Only network faults and 5xx/429 are retried, twice,
 * with the exponential backoff below.
 *
 * **`refetchOnWindowFocus: true`.** A player alt-tabs to a stream and back
 * mid-tournament; a bracket that is minutes old on return is worse than a
 * request nobody noticed. `staleTime` already stops this from being a
 * refetch storm — a focus event inside the stale window does nothing at
 * all.
 */
export const QUERY_STALE_TIME_MS = 30_000;
export const QUERY_GC_TIME_MS = 15 * 60_000;
export const QUERY_MAX_RETRIES = 2;

/** Exponential backoff, capped. Jittered by TanStack's own scheduler. */
function retryDelay(attemptIndex: number): number {
  return Math.min(1000 * 2 ** attemptIndex, 30_000);
}

function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= QUERY_MAX_RETRIES) return false;
  return normalizeError(error).isRetryable;
}

/**
 * The global error handler the task asks for.
 *
 * **Observes, never swallows.** A `useQuery` still returns its error and a
 * component still renders its own failure state; this exists so that no
 * failure is *only* visible in the component that happened to ask for it —
 * the same reason the backend logs at the boundary (CLAUDE.md §8.5).
 *
 * A cancelled request is not a failure and is not reported: a superseded
 * query is the cache working, and reporting it would bury real faults.
 */
function report(scope: "query" | "mutation", error: unknown): void {
  const normalized = error instanceof ApiError ? error : normalizeError(error);
  if (normalized.kind === "canceled") return;
  reportError(normalized, { scope, code: normalized.code, status: normalized.status });
}

/**
 * A factory, not a module-level singleton.
 *
 * One client per app instance means a test can build a fresh cache per
 * case without `clear()` calls that another test forgets — test isolation
 * by construction rather than by discipline (CLAUDE.md §6.7).
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({ onError: (error) => report("query", error) }),
    mutationCache: new MutationCache({ onError: (error) => report("mutation", error) }),
    defaultOptions: {
      queries: {
        staleTime: QUERY_STALE_TIME_MS,
        gcTime: QUERY_GC_TIME_MS,
        retry: shouldRetry,
        retryDelay,
        refetchOnWindowFocus: true,
        // The reconnect refetch is unconditional on purpose: coming back
        // online is the one moment the cache is most likely wrong.
        refetchOnReconnect: true,
      },
      mutations: {
        // A mutation is not idempotent unless its endpoint says so, and
        // this app cannot know which do. Retrying a blind POST is how a
        // player enters a tournament twice — CLAUDE.md §9.10.
        retry: false,
      },
    },
  });
}
