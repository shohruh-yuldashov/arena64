import { useEffect, useMemo, useRef } from "react";

import type { LobbyState, PendingMatch, QueueTicket } from "@/entities/queue";
import { isResolved } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { useMyTicket, usePendingMatch } from "@/features/matchmaking/model/queries";

/**
 * The lobby's whole state, derived from the two authoritative reads —
 * A64-020.5A §8, §9, §10.
 *
 * ## Why this is a hook and not a store
 *
 * §8 forbids duplicating it, and the reason is the pairing race. There is
 * a real window in which the server has produced a match and the ticket
 * read has not caught up, and a stored copy of "what the lobby is doing"
 * would be written during that window and then be wrong. Deriving on every
 * render means the union is a *view* of the cache: it cannot disagree with
 * the data it is computed from, because it has no independent existence.
 *
 * ## Precedence, in one place
 *
 * A pending match outranks a queue ticket, always. `entities/queue` argues
 * why; this is where it is applied, and it is applied **once** — a second
 * component that checked `ticket` before `match` would render "searching…"
 * over a live offer with a thirty-second deadline.
 *
 * ## The disappearing ticket
 *
 * A ticket that stops existing is the **only** signal a client gets that a
 * pairing may have happened, and it is the one moment the two polls can
 * miss each other. Their schedules are independent, so this ordering is
 * ordinary rather than unlucky:
 *
 *     t+0.0  the scan pairs; the ticket becomes `matched`
 *     t+0.4  the queue poll returns 404 -> no ticket
 *            the offer query stops polling, because nothing is queued
 *     ...    the offer is never read, and expires unanswered
 *
 * So the transition from *had a ticket* to *has none* refetches the offer
 * immediately. It costs one request per queue exit — including the ordinary
 * cancel, where it is exactly the reconciliation §13 asks for — and without
 * it a player is paired, told nothing, and loses the game to a deadline
 * they never saw. Observed against a real backend: the match was created
 * with the right clock and expired thirty seconds later.
 *
 * ## What is deliberately not derived here
 *
 * The four *busy* states — `joining`, `accepting`, `declining`,
 * `transitioning` — are not in this function's gift. They describe a
 * request this client started, which the cache cannot see, so the page
 * overlays them onto what this returns. Putting them here would mean
 * threading four mutation objects into a derivation whose whole point is
 * that it depends only on server state.
 */
export interface LobbyView {
  /** The derived state. Never stored, never written to. */
  state: LobbyState;
  /** Whether the two reads have answered at least once. */
  isLoading: boolean;
  /** Re-ask both. The recovery offered by the `unavailable` state. */
  refetch: () => void;
}

export function useLobbyState(): LobbyView {
  const { state: session } = useSession();

  // Read in this order on purpose. Each query decides its own polling
  // interval (§10), and the offer's decision needs to know whether a
  // ticket is live — a queued player must keep asking, or they learn they
  // were paired only when they refocus the tab. Reading the ticket first
  // supplies that without a store and without a render's lag.
  const ticket = useMyTicket();
  const pending = usePendingMatch(ticket.data != null);

  // See this module's docstring. `undefined` is "not answered yet" and is
  // not a disappearance, so only a real `ticket -> null` transition fires.
  const hadTicket = useRef(false);
  const pendingRefetch = pending.refetch;
  useEffect(() => {
    if (ticket.data === undefined) return;
    const has = ticket.data !== null;
    if (hadTicket.current && !has) void pendingRefetch();
    hadTicket.current = has;
  }, [ticket.data, pendingRefetch]);

  const derived = useMemo(
    () => derive({ session: isResolved(session), ticket: ticket.data, match: pending.data }),
    [session, ticket.data, pending.data],
  );

  const failed = ticket.isError && pending.isError;

  return {
    state: failed ? { status: "unavailable" } : derived,
    isLoading: ticket.isPending || pending.isPending,
    refetch: () => {
      void ticket.refetch();
      void pending.refetch();
    },
  };
}

/**
 * The pure half, exported for the test that asserts the precedence.
 *
 * A free function rather than inline, because "an offer beats a ticket" is
 * the rule this file exists for and a rule worth asserting directly is
 * worth being able to call directly.
 */
export function derive(input: {
  session: boolean;
  ticket: QueueTicket | null | undefined;
  match: PendingMatch | null | undefined;
}): LobbyState {
  if (!input.session) return { status: "bootstrapping" };

  // **First**, before anything looks at the ticket. See `entities/queue`.
  const match = input.match;
  if (match != null && isOpen(match)) {
    return match.you_accepted
      ? { status: "awaiting_opponent", match }
      : { status: "match_offer", match };
  }

  // An offer that has *settled* — the opponent declined, the window
  // closed, or both accepted — is not an offer any more. `active` is the
  // one settled status with somewhere to go; the rest fall through to
  // whatever the queue says, which after a decline is "not queued" and
  // after a requeue is a fresh ticket.
  if (match != null && match.status === "active") {
    return { status: "transitioning", matchId: match.match_id };
  }

  if (input.ticket != null) return { status: "queued", ticket: input.ticket };
  if (input.ticket === undefined || input.match === undefined) {
    return { status: "bootstrapping" };
  }
  return { status: "idle" };
}

/** Whether this offer is still waiting for an answer. */
function isOpen(match: PendingMatch): boolean {
  return match.status === "pending_acceptance";
}
