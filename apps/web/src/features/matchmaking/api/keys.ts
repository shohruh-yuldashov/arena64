/**
 * Every cache key the lobby owns — A64-020.5A §7.
 *
 * ## Three keys, not one
 *
 * The queue ticket and the pending match are **separate cache entries**,
 * deliberately. They are two endpoints with two lifetimes and two `404`
 * meanings, and merging them into one "lobby" object would mean a single
 * `setQueryData` after a join overwriting an offer that arrived between the
 * request and the response — the pairing race, lost in the cache layer
 * where nothing could see it.
 *
 * They are combined **above** the cache, by `useLobbyState`, into a
 * discriminated union that applies §8's precedence. Deriving is what makes
 * the precedence a rule rather than an ordering accident.
 *
 * ## The catalogue is not under `matchmaking`
 *
 * `["reference", "time-controls"]`, because that is whose data it is. A
 * key under `matchmaking` would be invalidated by a queue mutation, and a
 * queue mutation cannot change which clocks the platform offers — the
 * catalogue is seeded by a migration and edited by an operator, on a
 * timescale where a session-long cache is generous.
 *
 * ## The invalidation matrix
 *
 * | Mutation | Invalidates |
 * | --- | --- |
 * | join | `queue`, `pending` — a join can lose the QT-1 race, so both are re-read rather than trusted |
 * | cancel | `queue`, `pending` — cancellation may have lost to a pairing, and the offer must win |
 * | accept | `queue`, `pending` |
 * | decline | `queue`, `pending` — declining earns a cooldown that the next join reports |
 *
 * Every mutation touches the same two, which looks like it wants one key
 * and does not: what it means is that *any* write can be overtaken by the
 * pairing scan, so both authoritative reads are re-asked. Nothing here
 * invalidates the catalogue, and nothing outside this feature invalidates
 * these.
 */
export const matchmakingKeys = {
  root: ["matchmaking"] as const,

  /** `GET /matchmaking/queue/me` — the caller's live ticket, or `404`. */
  queue: () => [...matchmakingKeys.root, "queue", "me"] as const,

  /** `GET /matchmaking/matches/pending` — the caller's open offer, or `404`. */
  pending: () => [...matchmakingKeys.root, "matches", "pending"] as const,
} as const;

export const referenceKeys = {
  root: ["reference"] as const,

  /** `GET /time-controls` — the catalogue. Rarely stale; never invalidated here. */
  timeControls: () => [...referenceKeys.root, "time-controls"] as const,
} as const;
