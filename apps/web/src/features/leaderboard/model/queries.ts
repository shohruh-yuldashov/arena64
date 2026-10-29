import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import {
  type LeaderboardFilter,
  readLeaderboard,
  readMyStanding,
} from "@/features/leaderboard/api";
import { leaderboardKeys } from "@/features/leaderboard/api/keys";

/** Rows asked for per request. The server caps it at 100. */
export const LEADERBOARD_PAGE_SIZE = 50;

/**
 * The speed classes a ladder is offered for — A64-033.
 *
 * The four a player can actually be rated in (SPEC-RATING §8).
 * `correspondence` is a storable class nobody can reach yet, and a tab for
 * it would be a ladder that is empty by construction.
 */
export const LEADERBOARD_SPEED_CLASSES = ["bullet", "blitz", "rapid", "classical"] as const;

export type LeaderboardSpeedClass = (typeof LEADERBOARD_SPEED_CLASSES)[number];

export function isLeaderboardSpeedClass(value: unknown): value is LeaderboardSpeedClass {
  return (LEADERBOARD_SPEED_CLASSES as readonly unknown[]).includes(value);
}

/**
 * `null` while the page has not yet decided which class to show — it waits
 * for the viewer's own ratings rather than asking for a default ladder it
 * would replace a moment later.
 */
export function useLeaderboard(filter: LeaderboardFilter | null) {
  const { state } = useSession();

  return useInfiniteQuery({
    queryKey: leaderboardKeys.page(filter?.speedClass ?? "none", filter?.country ?? null),
    queryFn: ({ pageParam }) =>
      readLeaderboard(filter as LeaderboardFilter, {
        after: pageParam,
        limit: LEADERBOARD_PAGE_SIZE,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: filter !== null && isAuthenticated(state),
    // A ladder is read live on the server; a minute's staleness here is the
    // whole of its delay, and a refocus is enough to catch up.
    staleTime: 60_000,
  });
}

export function useMyStanding(filter: LeaderboardFilter | null) {
  const { state } = useSession();

  return useQuery({
    queryKey: leaderboardKeys.standing(filter?.speedClass ?? "none", filter?.country ?? null),
    queryFn: () => readMyStanding(filter as LeaderboardFilter),
    enabled: filter !== null && isAuthenticated(state),
    staleTime: 60_000,
  });
}
