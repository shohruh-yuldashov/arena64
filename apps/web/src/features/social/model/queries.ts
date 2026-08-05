import {
  type QueryClient,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { profileKeys } from "@/features/profile/api/keys";
import * as socialApi from "@/features/social/api";
import { MIN_QUERY_LENGTH } from "@/features/social/api";
import { socialKeys } from "@/features/social/api/keys";

/**
 * The social reads and writes — A64-020.4 §16.
 *
 * Every mutation invalidates a **named** set. See `socialKeys` for the
 * matrix and the reasoning; the helpers below are what enforce it, so a new
 * mutation copies a call rather than inventing a set.
 */

/** What a request-shaped mutation makes stale. */
function invalidateRequests(
  client: QueryClient,
  ...extra: readonly (readonly string[])[]
): void {
  for (const key of [
    socialKeys.searches(),
    socialKeys.friendCount(),
    profileKeys.publicAll(),
    ...extra,
  ]) {
    void client.invalidateQueries({ queryKey: key });
  }
}

/**
 * Player search, debounced by the **caller** and normalised here.
 *
 * ## Why the query key is the normalised term
 *
 * `" Ali "`, `"ali"` and `"ALI"` are one search to the API — it trims and
 * folds — so caching them under three keys would fetch the same page three
 * times and show three spinners. Normalising into the key is what makes
 * them one entry.
 *
 * ## Why nothing is cached for long
 *
 * `gcTime` is deliberately short. A search cache is keyed by arbitrary user
 * input, so it is unbounded by construction: a session that types twenty
 * terms holds twenty pages of profiles for as long as the default fifteen
 * minutes. Two minutes keeps back-navigation instant and lets the rest go.
 *
 * ## Cancellation
 *
 * TanStack passes an `AbortSignal` and Axios honours it, so a superseded
 * query's request is actually cancelled rather than merely ignored — which
 * matters on a mobile connection where the obsolete one is still in flight
 * when the next keystroke lands.
 */
export function useUserSearch(query: string) {
  const normalized = query.trim().toLowerCase();
  const enabled = normalized.length >= MIN_QUERY_LENGTH;

  return useInfiniteQuery({
    queryKey: socialKeys.search(normalized),
    queryFn: ({ pageParam, signal }) =>
      socialApi.searchPlayers(normalized, pageParam ?? undefined, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.page.next_cursor ?? undefined,
    // No request below the API's own floor — a one-character term is a
    // `422`, and asking for it is a round trip that can only fail.
    enabled,
    gcTime: 2 * 60_000,
  });
}

export function useFriends() {
  return useInfiniteQuery({
    queryKey: socialKeys.friends(),
    queryFn: ({ pageParam }) => socialApi.listFriends(pageParam ?? undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.page.next_cursor ?? undefined,
  });
}

export function useFriendCount() {
  return useQuery({ queryKey: socialKeys.friendCount(), queryFn: socialApi.countFriends });
}

export function useIncomingRequests() {
  return useInfiniteQuery({
    queryKey: socialKeys.incoming(),
    queryFn: ({ pageParam }) => socialApi.listIncoming(pageParam ?? undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.page.next_cursor ?? undefined,
  });
}

export function useOutgoingRequests() {
  return useInfiniteQuery({
    queryKey: socialKeys.outgoing(),
    queryFn: ({ pageParam }) => socialApi.listOutgoing(pageParam ?? undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.page.next_cursor ?? undefined,
  });
}

export function useBlockedPlayers() {
  return useInfiniteQuery({
    queryKey: socialKeys.blocked(),
    queryFn: ({ pageParam }) => socialApi.listBlocked(pageParam ?? undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.page.next_cursor ?? undefined,
  });
}

/**
 * Send a request.
 *
 * **Not optimistic.** The server can refuse for reasons the client cannot
 * predict — a block placed a moment ago, a request already in flight from
 * the other side — and an optimistic "Requested" that reverted would be
 * worse than a spinner. The relationship comes back from the refetch.
 */
export function useSendRequest() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (playerId: string) => socialApi.sendRequest(playerId),
    onSuccess: () => invalidateRequests(client, socialKeys.outgoing()),
  });
}

export function useCancelRequest() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (requestId: string) => socialApi.cancelRequest(requestId),
    onSuccess: () => invalidateRequests(client, socialKeys.outgoing()),
  });
}

export function useAcceptRequest() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (requestId: string) => socialApi.acceptRequest(requestId),
    // Accepting is the one request transition that creates a friendship, so
    // it is the one that makes the friends list stale.
    onSuccess: () => invalidateRequests(client, socialKeys.incoming(), socialKeys.friends()),
  });
}

export function useDeclineRequest() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (requestId: string) => socialApi.declineRequest(requestId),
    onSuccess: () => invalidateRequests(client, socialKeys.incoming()),
  });
}

export function useRemoveFriend() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (playerId: string) => socialApi.removeFriend(playerId),
    onSuccess: () => invalidateRequests(client, socialKeys.friends()),
  });
}

/**
 * Block, and invalidate **everything social**.
 *
 * The one mutation that earns a broad invalidation, because the write
 * genuinely is broad: blocking ends a friendship, declines pending requests
 * in both directions, and removes the target from search and every list.
 * Naming five keys here would be a list that goes stale the first time the
 * backend adds a consequence — `socialKeys.all` is the honest scope.
 *
 * Still scoped: it does not touch ratings, tournaments or the session.
 */
export function useBlockPlayer() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (playerId: string) => socialApi.blockPlayer(playerId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: socialKeys.all });
      void client.invalidateQueries({ queryKey: profileKeys.publicAll() });
    },
  });
}

/**
 * Unblock.
 *
 * Narrower than blocking on purpose: lifting a block restores *visibility*
 * and restores no relationship — the friendship it ended stays ended. So
 * the blocked list, search and the profile are stale, and the friends list
 * is not.
 */
export function useUnblockPlayer() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (playerId: string) => socialApi.unblockPlayer(playerId),
    onSuccess: () => {
      for (const key of [
        socialKeys.blocked(),
        socialKeys.searches(),
        profileKeys.publicAll(),
      ]) {
        void client.invalidateQueries({ queryKey: key });
      }
    },
  });
}
