import {
  type QueryClient,
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";

import * as challengeApi from "@/features/challenges/api";
import { challengeKeys } from "@/features/challenges/api/keys";
import { matchmakingKeys } from "@/features/matchmaking/api/keys";

/**
 * The challenge reads and writes — A64-022.5 §15.
 *
 * React Query and nothing else. There is no store, no context and no second
 * copy of a challenge anywhere: the lists **are** the cache, and every
 * mutation ends by invalidating a named set rather than by patching one.
 *
 * ## Why nothing polls
 *
 * §20. A challenge lives for twenty-four hours and changes when a person
 * does something, which is exactly the shape realtime is for: the
 * `notification.created` frame for either challenge type invalidates both
 * lists (`useChallengePush`), and the HTTP read decides what is actually
 * there. A one-minute poll on a twenty-four-hour object is a request per
 * minute to learn nothing.
 *
 * The one thing that *is* watched on this page is the pending match, and it
 * is watched by `matchmaking`'s own query with `matchmaking`'s own interval
 * — see `useChallengeHandoff`. Reusing that is what stops this feature
 * growing a second definition of "is the game ready".
 */

/** Both lists, stale at once. The frame does not say which side moved. */
export function invalidateChallenges(client: QueryClient): void {
  void client.invalidateQueries({ queryKey: challengeKeys.incoming() });
  void client.invalidateQueries({ queryKey: challengeKeys.outgoing() });
}

export function useIncomingChallenges() {
  return useInfiniteQuery({
    queryKey: challengeKeys.incoming(),
    queryFn: ({ pageParam }) => challengeApi.listIncoming(pageParam ?? undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.page.next_cursor ?? undefined,
  });
}

export function useOutgoingChallenges() {
  return useInfiniteQuery({
    queryKey: challengeKeys.outgoing(),
    queryFn: ({ pageParam }) => challengeApi.listOutgoing(pageParam ?? undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.page.next_cursor ?? undefined,
  });
}

/**
 * Send one.
 *
 * Invalidates **only** `outgoing`. The recipient's incoming list lives on
 * their client and no key here can reach it — what reaches it is the
 * notification this create produces, and that is the whole reason A64-022.4
 * exists.
 */
export function useCreateChallenge() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: challengeApi.createChallenge,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: challengeKeys.outgoing() });
    },
  });
}

/**
 * Accept one, and stop believing the lobby's cache.
 *
 * Two invalidations, and the second is the interesting one: acceptance
 * **created a match**, so `matchmaking`'s pending read is stale the instant
 * this returns. Invalidating it here is what makes the offer surface on
 * this page pick the match up without anything polling for it.
 *
 * The match id is on the response (`created_match_id`) and the caller uses
 * it directly — see `useChallengeHandoff`. Re-reading it from the pending
 * endpoint would work and is not done: the response is the authority for
 * the transaction that just committed, and a read could race a relay that
 * has not caught up.
 */
export function useAcceptChallenge() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: challengeApi.acceptChallenge,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: challengeKeys.incoming() });
      void client.invalidateQueries({ queryKey: matchmakingKeys.pending() });
    },
  });
}

export function useDeclineChallenge() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: challengeApi.declineChallenge,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: challengeKeys.incoming() });
    },
  });
}

export function useCancelChallenge() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: challengeApi.cancelChallenge,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: challengeKeys.outgoing() });
    },
  });
}
