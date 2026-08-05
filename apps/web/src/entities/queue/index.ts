import type { components } from "@/shared/api/generated/schema";

/**
 * The two things a lobby is ever looking at — A64-020.5A §8.
 *
 * Both are aliases over generated responses. Neither is re-declared: the
 * ticket's snapshot is `matchmaking.queue_ticket`'s and the offer's is
 * `game.match`'s, and a hand-written copy would drift the day either gains
 * a field.
 */
export type QueueTicket = components["schemas"]["QueueTicketResponse"];
export type PendingMatch = components["schemas"]["PendingMatchResponse"];
export type OpponentPreview = components["schemas"]["OpponentPreview"];

/** Which pool a ticket waits in. `ranked` moves a rating; `casual` does not. */
export type QueueType = components["schemas"]["QueueType"];
export type ProductVariant = components["schemas"]["ProductVariant"];

/**
 * Where the lobby is, as **one** value.
 *
 * A discriminated union rather than four booleans, for the reason
 * `SessionState` is one: nine states exist and only nine, and a
 * flags-encoding admits combinations that are meaningless and that some
 * component would eventually render — "queued and showing an offer", or
 * "idle while a join is in flight".
 *
 * ## It is *derived*, never stored
 *
 * §8 is explicit and the reason is a bug class rather than a preference. A
 * copy of this in a store is a second answer to "what is happening", and
 * the two disagree exactly when it matters most — during the pairing race,
 * when the server has already produced an offer and the client's copy still
 * says "waiting". So `useLobbyState` computes it from the two queries on
 * every render and nothing writes it down.
 *
 * ## `match_offer` outranks `queued`
 *
 * The single most important rule in this file, and it exists because the
 * backend is honest rather than atomic. Pairing consumes a ticket and
 * creates a match in **two transactions** (`PairingService` — a
 * cross-context call may not sit inside an open one), so a client polling
 * across the gap legitimately sees:
 *
 *     GET /queue/me   -> 404   the ticket is already matched
 *     GET /pending    -> 200   the offer exists
 *
 * and, a moment earlier, the reverse: a `reserved` ticket that still reads
 * as live beside a match that does not exist yet. Ordering the union so a
 * pending match always wins means the second reading can never overwrite
 * the first — the offer is the state with a deadline attached, and losing
 * it costs the player a game.
 */
export type LobbyState =
  /** The session has not resolved. Nothing has been asked for yet. */
  | { status: "bootstrapping" }
  /** Signed in, not queued, no offer. The form is showing. */
  | { status: "idle" }
  /** A join is in flight. */
  | { status: "joining" }
  /** A live ticket exists and no offer does. */
  | { status: "queued"; ticket: QueueTicket }
  /** An offer is open and must be answered. Outranks everything below. */
  | { status: "match_offer"; match: PendingMatch }
  /** This player has accepted; the opponent has not. The dialog stays up. */
  | { status: "awaiting_opponent"; match: PendingMatch }
  /** An answer is in flight. */
  | { status: "accepting"; match: PendingMatch }
  | { status: "declining"; match: PendingMatch }
  /** Both accepted. Navigating to the game. */
  | { status: "transitioning"; matchId: string }
  /** Neither read succeeded and retrying is the only option. */
  | { status: "unavailable" };

/** Whether the lobby is waiting on the server for something it started. */
export function isBusy(state: LobbyState): boolean {
  return (
    state.status === "joining" ||
    state.status === "accepting" ||
    state.status === "declining" ||
    state.status === "transitioning"
  );
}

/**
 * Whether the two reads should still be polled.
 *
 * §10 bounds polling to the states where the server's answer can change
 * *without this client doing anything* — waiting for an opponent, waiting
 * for a pairing. An idle lobby polls nothing: there is no ticket to be
 * paired and no offer to be answered, so two requests every two seconds
 * would be a heartbeat for a page that is not waiting for anything.
 */
export function isWatching(state: LobbyState): boolean {
  return (
    state.status === "queued" ||
    state.status === "match_offer" ||
    state.status === "awaiting_opponent"
  );
}

/**
 * The match a state is about, or `null`.
 *
 * Five of the nine carry one and the dialog is driven by all five, so
 * asking here beats five equality checks at the call site — and a tenth
 * state that also carried a match would be added in one place.
 */
export function matchOf(state: LobbyState): PendingMatch | null {
  switch (state.status) {
    case "match_offer":
    case "awaiting_opponent":
    case "accepting":
    case "declining":
      return state.match;
    default:
      return null;
  }
}
