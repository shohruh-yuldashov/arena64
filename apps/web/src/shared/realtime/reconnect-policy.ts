/**
 * How long to wait before trying again — A64-020.5B §6.
 *
 * ## The numbers, and why each one
 *
 * | | Value | Why |
 * | --- | --- | --- |
 * | base | 500 ms | Below human perception for the common case — a laptop waking, a proxy recycling a connection. A first retry a player notices is one that makes a recoverable blip feel like an outage. |
 * | factor | 2 | Doubling. The standard, and the only one that reaches a sane ceiling in a small number of attempts. |
 * | ceiling | 15 s | A live game is on the other side of this socket. A minute-long ceiling would be correct for a background feed and is wrong here: a player staring at a frozen board will reload the page long before it fires, which costs the server a full snapshot instead of a resume. |
 * | jitter | ±25 % full | Applied to the whole delay, not added to it. Two players whose game reconnects them simultaneously — which is exactly what a gateway restart produces — must not retry in lockstep. |
 * | attempts | unbounded | Bounded backoff, **unbounded attempts**: the ceiling is what stops the tight loop §6 forbids, and giving up entirely on a game in progress is worse than retrying every fifteen seconds. A player who wants to stop closes the tab. |
 *
 * The sequence is therefore roughly 0.5, 1, 2, 4, 8, 15, 15, … seconds,
 * each ±25 %.
 *
 * ## What resets it
 *
 * A **stable** ready state, not merely reaching `ready`. A socket that
 * connects, authenticates and dies a second later has not recovered, and
 * resetting on `connection.ready` would turn a flapping gateway into an
 * unbacked-off retry loop at exactly the moment backoff matters most.
 * `STABLE_AFTER_MS` is what "stable" means.
 */
export const RECONNECT_BASE_MS = 500;
export const RECONNECT_FACTOR = 2;
export const RECONNECT_CEILING_MS = 15_000;
export const RECONNECT_JITTER = 0.25;

/**
 * How long a connection must hold before the backoff is forgiven.
 *
 * Ten seconds: comfortably longer than a handshake plus the first snapshot,
 * and short enough that a player who genuinely recovered is not carrying a
 * long delay into their next blip.
 */
export const STABLE_AFTER_MS = 10_000;

/**
 * The delay before attempt `attempt` (1-based), in milliseconds.
 *
 * `random` is injected so the schedule is a pure function in tests — AD-07's
 * rule for clocks, applied to the other source of nondeterminism.
 */
export function reconnectDelay(attempt: number, random: () => number = Math.random): number {
  const uncapped = RECONNECT_BASE_MS * RECONNECT_FACTOR ** Math.max(0, attempt - 1);
  const capped = Math.min(uncapped, RECONNECT_CEILING_MS);
  // Jitter spreads **around** the delay and is then clamped, so the ceiling
  // is genuinely a ceiling. Spreading without the clamp would put the
  // longest waits 25 % above it — which the first version did, and which
  // the test caught by asserting the invariant the docstring claimed.
  const spread = capped * RECONNECT_JITTER;
  const jittered = capped - spread + random() * spread * 2;
  return Math.round(Math.min(jittered, RECONNECT_CEILING_MS));
}
