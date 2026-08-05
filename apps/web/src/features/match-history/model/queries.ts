import { useInfiniteQuery } from "@tanstack/react-query";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { readMatchHistory } from "@/features/match-history/api";
import { matchHistoryKeys } from "@/features/match-history/api/keys";

/**
 * A player's finished matches, page by page — A64-020.5F §16.
 *
 * ## `useInfiniteQuery` over the backend's opaque cursor
 *
 * The cursor is a base64 pair of server-side columns and this client never
 * looks inside it: `getNextPageParam` returns what the last page said and
 * the next request sends it back verbatim. Decoding it to build an offset
 * would couple the client to the endpoint's ordering, which is precisely
 * what an opaque cursor exists to prevent — and offsets are forbidden
 * outright (§16), because a match completing mid-scroll shifts every one of
 * them and silently duplicates or skips a row.
 *
 * `null` on the last page, which is what stops the chain: `undefined` from
 * `getNextPageParam` is what TanStack reads as "no more".
 *
 * ## One request per page and none per row
 *
 * The opponent and the time control are composed by the backend into the
 * same response, so a row renders from what it was given. §17 forbids a
 * profile lookup per row and this could not make one — there is no
 * per-entry query here to make.
 *
 * ## Why the page size is 20 and not larger
 *
 * The endpoint caps at 100 and defaults to 20. A history page is a list a
 * player scans rather than reads, so the first screen matters and the
 * hundredth row does not; twenty fills a viewport with room to scroll,
 * which is the signal that "load more" exists.
 */
export const HISTORY_PAGE_SIZE = 20;

export function useMatchHistory(playerId: string | null) {
  const { state } = useSession();

  return useInfiniteQuery({
    queryKey: matchHistoryKeys.player(playerId ?? "none"),
    queryFn: ({ pageParam }) =>
      readMatchHistory(playerId ?? "", {
        after: pageParam,
        limit: HISTORY_PAGE_SIZE,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: isAuthenticated(state) && playerId !== null,
    // A finished match never changes, so a page already held is still
    // true. What *can* change is that a new match joined the front of the
    // list — which a focus refetch would catch, and which the game's own
    // completion already invalidates (§25).
    staleTime: 30_000,
  });
}
