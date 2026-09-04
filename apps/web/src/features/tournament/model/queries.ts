import {
  type QueryClient,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { isAuthenticated, isResolved } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { profileKeys } from "@/features/profile/api/keys";
import type { TournamentFilters } from "@/features/tournament/api";
import * as tournamentApi from "@/features/tournament/api";
import { tournamentKeys } from "@/features/tournament/api/keys";
import { isMoving } from "@/features/tournament/model/bracket";
import { isAmbiguousFailure } from "@/features/tournament/model/errors";

/**
 * The tournament reads and writes — A64-020.6 §4, §9, §10, §19.
 *
 * ## Everything is gated on a resolved session
 *
 * Every tournament route on the backend sits behind `CurrentUser`,
 * including the "public" reads: §7 of the tournament spec means *no viewer
 * is narrower than another*, not *anonymous*. So a query that fired during
 * `bootstrapping` would run without an access token, take a `401`, and
 * cache the failure as an outage on a page the player is entitled to see —
 * the bug A64-020.3 shipped and A64-020.4 fixed.
 *
 * ## Polling, and the honest name for it — §19
 *
 * **There is no tournament realtime protocol.** `app/gateway/protocol.py`
 * publishes exactly three channels — `system`, `matchmaking` and `game` —
 * and none of them carries a bracket. Nothing here claims otherwise, and
 * nothing here opens a second socket to invent one (§19 forbids both).
 *
 * So a tournament that is still moving is re-read on a bounded interval,
 * and one that has finished is not re-read at all: a completed tournament's
 * bracket is immutable and its standings were materialised once, so polling
 * it is a request per interval that cannot return anything new.
 *
 *     in progress / closed     POLL_INTERVAL_MS, detail and bracket only
 *     completed / cancelled    never
 *     the lobby                on focus, like every other list
 *     standings                never — they exist only once, at completion
 *
 * One interval, applied to two queries that must agree. This is the
 * limitation A64-021 Notifications or a later realtime phase removes.
 */

/**
 * Eight seconds — the middle of §19's 5–10 range.
 *
 * A tournament match is minutes long, so the thing being waited for is a
 * round advancing rather than a move landing; at eight seconds a player
 * watching a bracket sees a result within one breath of it happening, and
 * a page left open for an hour costs 450 requests rather than 720.
 */
export const POLL_INTERVAL_MS = 8_000;

/**
 * What to re-read after a registration write failed — §9.
 *
 * The entry, always: a `409` is the server telling this client its picture
 * was wrong, and the picture is what the panel renders.
 *
 * The **tournament as well** when the failure was ambiguous. A network
 * fault after the request left this machine is indistinguishable from one
 * before it, so the write may have landed — and if it did, the entrant
 * count moved too. §9's rule is to refetch rather than to press the button
 * again, and refetching only half the state is how a player ends up
 * registered on a page that says fourteen of sixteen.
 *
 * Not `refetchQueries`: invalidating marks them stale and TanStack re-reads
 * the ones actually mounted, so a panel the player has already navigated
 * away from costs nothing.
 */
function reconcile(client: QueryClient, tournamentId: string, error: unknown): void {
  void client.invalidateQueries({ queryKey: tournamentKeys.myRegistration(tournamentId) });
  if (isAmbiguousFailure(error)) {
    void client.invalidateQueries({ queryKey: tournamentKeys.detail(tournamentId) });
  }
}

/** One lobby page. Twenty fills a viewport and leaves room to scroll. */
export const LOBBY_PAGE_SIZE = 20;

export function useTournaments(filters: TournamentFilters = {}) {
  const { state } = useSession();

  return useInfiniteQuery({
    queryKey: tournamentKeys.list(filters),
    queryFn: ({ pageParam }) =>
      tournamentApi.readTournaments(filters, {
        after: pageParam,
        limit: LOBBY_PAGE_SIZE,
      }),
    initialPageParam: null as string | null,
    // The cursor the server issued, sent back unread. `undefined` is what
    // TanStack reads as "no more pages".
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: isResolved(state),
    // A lobby changes when somebody creates or starts a tournament, which
    // is rare and which a focus refetch catches. Not polled — §19.
    staleTime: 30_000,
  });
}

export function useTournament(tournamentId: string) {
  const { state } = useSession();

  return useQuery({
    queryKey: tournamentKeys.detail(tournamentId),
    queryFn: () => tournamentApi.readTournament(tournamentId),
    enabled: isResolved(state),
    // Polls itself, because it is what decides whether to keep polling: the
    // status that stops the interval arrives on this response.
    refetchInterval: (query) =>
      query.state.data && isMoving(query.state.data.status) ? POLL_INTERVAL_MS : false,
  });
}

/**
 * The bracket, polled while the tournament is moving.
 *
 * `status` comes from the caller rather than from a second read of the
 * detail: two queries deciding independently whether to poll would drift,
 * and one of them would keep asking after the other had stopped.
 */
export function useBracket(tournamentId: string, status: string | undefined) {
  const { state } = useSession();

  return useQuery({
    queryKey: tournamentKeys.bracket(tournamentId),
    queryFn: () => tournamentApi.readBracket(tournamentId),
    enabled: isResolved(state),
    refetchInterval: status !== undefined && isMoving(status) ? POLL_INTERVAL_MS : false,
  });
}

/**
 * The final placement — read **only** once a tournament has completed.
 *
 * Not merely "not polled": not requested at all before then. The endpoint
 * answers with an empty list while a tournament is being played (standings
 * are materialised once, at completion), so asking early is a request whose
 * answer is known in advance.
 */
export function useStandings(tournamentId: string, status: string | undefined) {
  const { state } = useSession();

  return useQuery({
    queryKey: tournamentKeys.standings(tournamentId),
    queryFn: () => tournamentApi.readStandings(tournamentId),
    enabled: isResolved(state) && status === "completed",
    // Immutable once written. Nothing will change this again.
    staleTime: Infinity,
  });
}

/**
 * The viewer's own entry, or `null` for "never entered".
 *
 * The **authoritative** participant state (§8). Nothing on this page infers
 * whether the viewer is registered from which controls rendered, because
 * that inverts the authority — the record is the server's, and this is the
 * read of it.
 */
export function useMyRegistration(tournamentId: string) {
  const { state } = useSession();

  return useQuery({
    queryKey: tournamentKeys.myRegistration(tournamentId),
    queryFn: () => tournamentApi.readMyRegistration(tournamentId),
    // A64-026.4 §43.4. `isAuthenticated`, not `isResolved`: the tournament
    // reads are open to a visitor with no account, but "am I in this one"
    // has no anonymous answer and the endpoint keeps its token. Firing it
    // for an anonymous viewer would spend a request to be told 401 on every
    // public tournament page.
    enabled: isAuthenticated(state),
  });
}

/**
 * Enter a tournament — §9.
 *
 * **Not optimistic.** §9 forbids inventing a registration row, and the
 * reason is not tidiness: the server decides whether the field is full
 * under a row lock, so an optimistic entry would show a player as
 * registered in the one case that matters — the race it lost.
 *
 * The response *is* the written entry, so it is seeded straight into the
 * cache rather than re-fetched, and the detail is invalidated because the
 * entrant count moved. On failure the entry is re-read: an ambiguous
 * network fault leaves the truth on the server, and §9's rule is to refetch
 * rather than to press the button again.
 */
export function useEnterTournament(tournamentId: string) {
  const client = useQueryClient();

  return useMutation({
    mutationFn: () => tournamentApi.enterTournament(tournamentId),
    onSuccess: (registration) => {
      client.setQueryData(tournamentKeys.myRegistration(tournamentId), registration);
      void client.invalidateQueries({ queryKey: tournamentKeys.detail(tournamentId) });
      void client.invalidateQueries({ queryKey: tournamentKeys.lists() });
      // The profile's tournament history gained a row. Invalidated narrowly
      // rather than through `profileKeys.all`, which would also discard the
      // account, the ratings and the privacy settings (§26).
      void client.invalidateQueries({ queryKey: [...profileKeys.all, "tournaments"] });
    },
    onError: (error) => reconcile(client, tournamentId, error),
  });
}

/**
 * Withdraw — §10.
 *
 * Allowed **only before registration closes**, and the server is what
 * enforces that: after the field is fixed the bracket is built from exactly
 * those players, so a late withdrawal would leave a seat nothing fills. It
 * is refused rather than converted to a forfeit, because a forfeit is a
 * match outcome and there is no match yet.
 *
 * The row survives as `withdrawn`, which is why the response is seeded like
 * the entry's: "you left this one" is a state to render, not an absence.
 */
export function useWithdrawFromTournament(tournamentId: string) {
  const client = useQueryClient();

  return useMutation({
    mutationFn: () => tournamentApi.withdrawFromTournament(tournamentId),
    onSuccess: (registration) => {
      client.setQueryData(tournamentKeys.myRegistration(tournamentId), registration);
      void client.invalidateQueries({ queryKey: tournamentKeys.detail(tournamentId) });
      void client.invalidateQueries({ queryKey: tournamentKeys.lists() });
      void client.invalidateQueries({ queryKey: [...profileKeys.all, "tournaments"] });
    },
    onError: (error) => reconcile(client, tournamentId, error),
  });
}
