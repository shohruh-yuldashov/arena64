import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";

import { matchmakingKeys } from "@/features/matchmaking/api/keys";
import { type InboundFrame, useFrames } from "@/shared/realtime";

/**
 * Match offers, pushed — A64-020.5D §5, §6.
 *
 * The lobby polled at two seconds because `LoggingPendingMatchSink` never
 * put a pairing on a socket. It does now, and this is the client half:
 * `matchmaking.match.offered` arrives on the one shared socket and the
 * lobby stops waiting for its next tick.
 *
 * ## The push is a wake-up signal, not an answer
 *
 * §6 is explicit and the reason is §3: the frame may be duplicated, may
 * arrive late, and may be missed entirely while the socket was down. So
 * **nothing here trusts the payload**. It carries a safe preview of a match
 * card, and what this hook does with it is invalidate the authoritative
 * read — which then decides whether the offer still exists, whether the
 * deadline is valid, whether this player may answer, and whether the match
 * has already started.
 *
 * A client that rendered the payload directly would show a dialog for an
 * offer the opponent declined a second earlier.
 *
 * ## Duplicates collapse into one refetch
 *
 * §6 asks for single-flight or bounded deduplication, and the reason is
 * concrete: a reconnect can replay several frames for one match at once,
 * and three pushes must not be three `GET`s. The guard is the match id plus
 * an in-flight flag — the *same* match seen twice does nothing, and a
 * different one is genuinely different news.
 *
 * ## Why this is a hook and not a provider
 *
 * §5 prefers an app-level subscription, and the honest position is that
 * this app has no app-level match state to record one into: the lobby's
 * truth is a TanStack Query cache keyed by `matchmakingKeys`, and there is
 * nothing above `/play` that renders a match offer. So it is mounted by the
 * lobby, and the limitation is documented rather than papered over: a
 * player on `/profile` when they are paired learns on their next visit to
 * `/play`, exactly as they did before — the durable read is unchanged.
 *
 * Closing that gap means an app-level offer surface, which §1 excludes
 * ("do not implement notifications").
 */

/** Match ids already reconciled, bounded so a long session cannot grow it. */
const REMEMBERED = 16;

export function useMatchOfferPush(): void {
  const client = useQueryClient();

  // The ids this hook has already acted on, newest last. A ring rather
  // than a Set that grows: a session that plays fifty games would
  // otherwise accumulate fifty ids to answer a question only the last few
  // can be asked.
  const seen = useRef<string[]>([]);
  const inFlight = useRef(false);

  useFrames(
    useCallback(
      (frame: InboundFrame) => {
        if (frame.type !== "matchmaking.match.offered") return;

        const matchId = frame.payload.match_id;
        if (typeof matchId !== "string") return;

        // A duplicate of something already reconciled. Harmless by design
        // (§3) and dropped here so it costs nothing.
        if (seen.current.includes(matchId)) return;
        seen.current = [...seen.current, matchId].slice(-REMEMBERED);

        // Single-flight: several distinct offers arriving together — a
        // reconnect replaying a backlog — collapse into one refetch,
        // because one read answers all of them.
        if (inFlight.current) return;
        inFlight.current = true;

        // **Both keys**, not only the pending one. A pairing consumes the
        // queue ticket, so a lobby that refreshed the offer and kept a
        // stale ticket would show a match card above a "searching" line.
        // The same reasoning `invalidateLobby` records.
        void Promise.all([
          client.invalidateQueries({ queryKey: matchmakingKeys.pending() }),
          client.invalidateQueries({ queryKey: matchmakingKeys.queue() }),
        ]).finally(() => {
          inFlight.current = false;
        });
      },
      [client],
    ),
  );
}
