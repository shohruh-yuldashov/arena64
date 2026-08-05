/**
 * Where the socket is — A64-020.5B §5.
 *
 * A bounded state machine, and a **closed union** rather than a set of
 * booleans, for the reason `SessionState` and `LobbyState` are: eight
 * states exist and only eight, and a flags encoding admits combinations
 * that are meaningless — "connecting and ready", "closed and reconnecting"
 * — which some component would eventually render.
 *
 * ## What the UI is allowed to see
 *
 * §5 says "expose only stable derived state". Components read
 * `ConnectionStatus` and nothing else: no `WebSocket`, no attempt counters
 * mid-flight, no error objects. The transport's callbacks fire at times
 * React knows nothing about, and letting them set component state directly
 * is how a socket ends up re-rendering a board on every heartbeat.
 *
 * ## Why `offline` is distinct from `reconnecting`
 *
 * `navigator.onLine` said the network is gone. Backing off against a
 * machine with no network is a timer that will fail predictably, so the
 * policy pauses instead — and the distinction is visible to the player,
 * because "you are offline" and "we are reconnecting" ask for different
 * things from them.
 *
 * ## Why `fatal` is distinct from `closed`
 *
 * `closed` is "we stopped on purpose" — a sign-out, a page leaving. `fatal`
 * is "the server refused us in a way retrying cannot fix", which today
 * means an authentication rejection. Retrying either would be a loop, and
 * conflating them would make a sign-out look like a bug.
 */
export type ConnectionStatus =
  /** Nothing has started. No session yet, or nothing has asked for a socket. */
  | "idle"
  /** Minting a one-time ticket over HTTP. */
  | "ticketing"
  /** The socket is opening, or open and not yet authenticated. */
  | "connecting"
  /** `connection.ready` arrived. The only state in which frames may be sent. */
  | "ready"
  /** The socket dropped and a backoff is running. */
  | "reconnecting"
  /** The browser says there is no network. Attempts are paused. */
  | "offline"
  /** Stopped deliberately — sign-out, teardown. Nothing will retry. */
  | "closed"
  /** Refused in a way retrying cannot fix. Nothing will retry. */
  | "fatal";

/** Whether frames may be sent right now. */
export function isReady(status: ConnectionStatus): boolean {
  return status === "ready";
}

/**
 * Whether the client is trying, or would try, to be connected.
 *
 * `idle` is excluded: it means nothing has asked yet. The UI uses this to
 * decide between "connection lost, hold on" and "not connected", which are
 * different sentences.
 */
export function isPursuing(status: ConnectionStatus): boolean {
  return status === "ticketing" || status === "connecting" || status === "reconnecting";
}

/** Whether this is a resting state that nothing will move on its own. */
export function isTerminal(status: ConnectionStatus): boolean {
  return status === "closed" || status === "fatal";
}
