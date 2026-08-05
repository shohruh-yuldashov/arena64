import { type QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { isResolved } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import * as matchmakingApi from "@/features/matchmaking/api";
import { matchmakingKeys, referenceKeys } from "@/features/matchmaking/api/keys";

/**
 * The lobby's reads and writes — A64-020.5A §9, §10, §11.
 *
 * ## Nothing is asked for before the session resolves
 *
 * Every query here is gated on `isResolved(session)`. That is not caution;
 * it is the bug A64-020.3 shipped and A64-020.4 fixed: a query that fires
 * during `bootstrapping` runs without an access token, gets a `401`, and
 * caches the failure — which then renders as an outage on a page the user
 * is perfectly entitled to see. `RequireAuth` already turns anonymous
 * visitors away, so by the time these run the only two outcomes are
 * "authenticated" and "the server could not be reached".
 *
 * ## Polling is temporary and is written down as such
 *
 * The backend's realtime seam is real but unwired: `PendingMatchSink` is
 * satisfied by `LoggingPendingMatchSink`, so a pairing reaches a log line
 * and no socket. `GET /matchmaking/matches/pending` is deliberately **not
 * rate limited** for exactly this reason — the backend's own docstring
 * says a client that only polls "still works correctly; it simply learns
 * later".
 *
 * So this phase polls, at two seconds, and only while the lobby is
 * genuinely waiting for something it cannot cause. When the gateway is
 * connected, `refetchInterval` goes to `false` and the socket invalidates
 * these keys instead — the queries, the components and the state model do
 * not change. See `specs/frontend.md` §16.
 */

/** How often the two authoritative reads are re-asked while waiting. */
export const POLL_INTERVAL_MS = 2_000;

/**
 * The catalogue.
 *
 * Cached for the session and never invalidated by anything in this
 * feature. A control is retired by an operator editing a row, not by
 * anything a player does, so a mutation that invalidated this would be
 * refetching four rows to observe a change no queue action can produce.
 *
 * `staleTime: Infinity` rather than a long number, because "how long until
 * this is worth re-asking" has no honest answer short of "when the page
 * reloads". A player who leaves a tab open across a catalogue change sends
 * a retired identifier once, gets `422 unsupported_time_control`, and the
 * error handler tells them to reload — which is the recovery §21 already
 * maps and is cheaper than a heartbeat on every session.
 */
export function useTimeControls() {
  const { state } = useSession();
  return useQuery({
    queryKey: referenceKeys.timeControls(),
    queryFn: matchmakingApi.listTimeControls,
    enabled: isResolved(state),
    staleTime: Infinity,
  });
}

/**
 * The caller's live ticket, or `null`.
 *
 * `staleTime: 0` — a ticket can be consumed by a pairing scan at any
 * instant, so there is no window in which a cached copy is known good.
 */
export function useMyTicket() {
  const { state } = useSession();
  return useQuery({
    queryKey: matchmakingKeys.queue(),
    queryFn: matchmakingApi.readMyTicket,
    enabled: isResolved(state),
    staleTime: 0,
    // Self-deciding, from this query's own data: a live ticket may be
    // consumed by a pairing scan at any instant, so it is watched; nothing
    // is watched when there is no ticket, because a lobby showing the join
    // form is not waiting for anything the server can do on its own.
    refetchInterval: (query) => (query.state.data == null ? false : POLL_INTERVAL_MS),
  });
}

/**
 * The caller's open offer, or `null`.
 *
 * Polled in **two** situations, which is why this one is told about the
 * ticket rather than deciding alone:
 *
 *   - an offer is open and the opponent has not answered
 *   - a ticket is live, so a pairing could produce an offer at any moment
 *
 * The second is the one that cannot be seen from here. Omitting it would
 * mean a queued player never learning they had been paired until they
 * refocused the tab — the pairing race, lost by not looking.
 *
 * `hasTicket` comes from the ticket query, which is read first in
 * `useLobbyState`, so there is no circularity and no one-render lag.
 */
export function usePendingMatch(hasTicket: boolean) {
  const { state } = useSession();
  return useQuery({
    queryKey: matchmakingKeys.pending(),
    queryFn: matchmakingApi.readPendingMatch,
    enabled: isResolved(state),
    staleTime: 0,
    refetchInterval: (query) => {
      const match = query.state.data;
      const open = match != null && match.status === "pending_acceptance";
      return open || hasTicket ? POLL_INTERVAL_MS : false;
    },
  });
}

/**
 * Re-ask both authoritative reads.
 *
 * The recovery every mutation ends with, and the reason it is one function:
 * **any** write on this page can be overtaken by the pairing scan, so the
 * honest response to finishing one is to stop believing the cache. A
 * mutation that invalidated only "its own" key would be asserting that it
 * knows which of the two moved, which during a pairing it does not.
 */
function reconcile(client: QueryClient): void {
  void client.invalidateQueries({ queryKey: matchmakingKeys.queue() });
  void client.invalidateQueries({ queryKey: matchmakingKeys.pending() });
}

/**
 * Join a pool.
 *
 * **Not optimistic.** The server refuses for reasons this client cannot
 * predict — a ticket another tab created a moment ago, a cooldown from a
 * decline, a control retired since the catalogue was read — and an
 * optimistic "searching…" that reverted would be worse than a spinner. The
 * ticket comes back from the response and is seeded straight into the
 * cache, so the transition to `queued` costs no extra round trip.
 *
 * On **any** outcome, including failure, both reads are reconciled. A
 * `409` in particular is not a fatal error: it means the authoritative
 * state is something other than what this tab believed, which is precisely
 * what a refetch resolves (§11).
 */
export function useJoinQueue() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: matchmakingApi.joinQueue,
    onSuccess: (ticket) => {
      client.setQueryData(matchmakingKeys.queue(), ticket);
      void client.invalidateQueries({ queryKey: matchmakingKeys.pending() });
    },
    onError: () => reconcile(client),
  });
}

/**
 * Leave the queue.
 *
 * Reconciles rather than clearing, and §13 is why: `DELETE` answers `204`
 * whether or not there was a ticket, so a `204` does **not** mean "you are
 * idle" — it means "you are not queued", which is also true of a player
 * whose ticket was just consumed by a pairing. Writing `null` into the
 * queue key and calling it done would show "cancelled" to somebody who has
 * a live offer waiting.
 */
export function useLeaveQueue() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: matchmakingApi.leaveQueue,
    onSettled: () => reconcile(client),
  });
}

/**
 * Accept.
 *
 * The response is the match in its new state, so it is seeded rather than
 * refetched — which is what lets the dialog switch from "accept" to
 * "waiting for your opponent" without a round trip. The queue key is
 * invalidated too, because a settled match resolves its tickets.
 *
 * `onError` reconciles, and that is the important half: a network failure
 * after the server committed is indistinguishable from one before it, and
 * the only safe response is to re-read rather than to let the player press
 * the button again (§16).
 */
export function useAcceptMatch() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: matchmakingApi.acceptMatch,
    onSuccess: (match) => {
      client.setQueryData(matchmakingKeys.pending(), match);
      void client.invalidateQueries({ queryKey: matchmakingKeys.queue() });
    },
    onError: () => reconcile(client),
  });
}

/** Decline. Same shape; the match comes back `cancelled`. */
export function useDeclineMatch() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: matchmakingApi.declineMatch,
    onSuccess: (match) => {
      client.setQueryData(matchmakingKeys.pending(), match);
      void client.invalidateQueries({ queryKey: matchmakingKeys.queue() });
    },
    onError: () => reconcile(client),
  });
}
