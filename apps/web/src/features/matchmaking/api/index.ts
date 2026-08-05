import type { PendingMatch, QueueTicket } from "@/entities/queue";
import type { TimeControl } from "@/entities/time-control";
import { api, ApiError } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * Every matchmaking call, in one file — A64-020.5A §3.
 *
 * A URL, a generated payload type, nothing else. **No call names a player.**
 * The queueing, cancelling, accepting and declining account all come from
 * the access token, which is why none of these functions takes an identity
 * and why there is no ownership check anywhere above them: the thing one
 * would guard against is not expressible.
 *
 * `matchId` is the only identifier a caller supplies, and it comes from a
 * response this client received rather than from a form.
 */
type Schemas = components["schemas"];

export type JoinQueueRequest = Schemas["JoinQueueRequest"];

/**
 * The catalogue.
 *
 * Read once per session and cached for it — see `useTimeControls`. Never
 * assembled locally: §4 forbids inferring `base_time_ms` from an
 * identifier, and the reason is that doing so puts a second definition of
 * every control in the client.
 */
export function listTimeControls(): Promise<TimeControl[]> {
  return api.get<TimeControl[]>("/time-controls");
}

/**
 * The caller's live ticket, or `null`.
 *
 * `404` is **not an error here**: a player who is not queued is the
 * ordinary case, and the API says so identically whether they never
 * joined, left, were paired or expired. Translating it to `null` at the
 * boundary is what lets every caller branch on a value instead of on an
 * exception — and what stops React Query treating "you are not in a queue"
 * as a failure worth retrying.
 *
 * Every other failure propagates. A `500` is not an absence.
 */
export function readMyTicket(): Promise<QueueTicket | null> {
  return absentOn404(api.get<QueueTicket>("/matchmaking/queue/me"));
}

/** The caller's open offer, or `null`. Same `404` contract as above. */
export function readPendingMatch(): Promise<PendingMatch | null> {
  return absentOn404(api.get<PendingMatch>("/matchmaking/matches/pending"));
}

export function joinQueue(payload: JoinQueueRequest): Promise<QueueTicket> {
  return api.post<QueueTicket>("/matchmaking/queue", payload);
}

/**
 * Leave the queue.
 *
 * `204` whether or not there was a ticket — the API is deliberately
 * idempotent here, so a second tab's cancellation is not reported to this
 * one as an error. What it does *not* tell us is whether a pairing won the
 * race, which is why the caller re-reads both endpoints rather than
 * assuming idle (§13).
 */
export function leaveQueue(): Promise<void> {
  return api.delete<void>("/matchmaking/queue");
}

export function acceptMatch(matchId: string): Promise<PendingMatch> {
  return api.post<PendingMatch>(`/matchmaking/matches/${matchId}/accept`);
}

export function declineMatch(matchId: string): Promise<PendingMatch> {
  return api.post<PendingMatch>(`/matchmaking/matches/${matchId}/decline`);
}

/**
 * `404` as absence, everything else as a failure.
 *
 * Written once because both reads need it identically and because getting
 * it wrong in one of them is invisible: a `readPendingMatch` that threw on
 * `404` would make an un-paired lobby show a retry screen, which looks
 * exactly like a backend outage.
 */
async function absentOn404<T>(pending: Promise<T>): Promise<T | null> {
  try {
    return await pending;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}
