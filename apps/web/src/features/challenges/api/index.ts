import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * Every friend challenge call, in one file — A64-022.5 §4, §21.
 *
 * A URL, a generated payload type, nothing else. The types are the
 * **generated** ones rather than hand-written interfaces: the backend has
 * owned this contract since A64-022.2 and a second declaration here would
 * be a copy that drifts the first time a field is added.
 *
 * ## No call names the sender
 *
 * The challenger, the decliner and the canceller are all the access token.
 * `recipient_id` is the one identity a caller supplies, and it is a
 * `player_id` **the server previously returned** — from the friends list or
 * from a profile — never a username somebody typed.
 *
 * ## The two lists are separate endpoints, not one filtered read
 *
 * `incoming` and `outgoing` answer different questions and carry different
 * actions, and merging them would mean a client deciding which side of a
 * challenge it is on by comparing ids — a comparison the server has already
 * made.
 */
type Schemas = components["schemas"];

export type Challenge = Schemas["ChallengeResponse"];
export type ChallengePage = Schemas["CursorPage_ChallengeResponse_"];
export type CreateChallengeRequest = Schemas["CreateChallengeRequest"];
export type ChallengeStatus = Schemas["ChallengeStatus"];

/**
 * The rule set every challenge is played under.
 *
 * `ProductVariant` has exactly one member, so the dialog offers no choice —
 * a radio group with one option is a control that can only be left where it
 * was. The field is still sent explicitly rather than omitted, because the
 * generated request requires it, and it is typed from the **generated
 * union** rather than written as a string: the day a second variant is
 * added, this constant is the one place the compiler points at, and the
 * decision to make it a control becomes a visible one.
 */
export const DEFAULT_VARIANT: CreateChallengeRequest["variant"] = "russian_8x8";

export function listIncoming(cursor?: string): Promise<ChallengePage> {
  return api.get<ChallengePage>("/challenges/incoming", {
    params: cursor === undefined ? undefined : { cursor },
  });
}

export function listOutgoing(cursor?: string): Promise<ChallengePage> {
  return api.get<ChallengePage>("/challenges/outgoing", {
    params: cursor === undefined ? undefined : { cursor },
  });
}

export function createChallenge(payload: CreateChallengeRequest): Promise<Challenge> {
  return api.post<Challenge>("/challenges", payload);
}

/**
 * Accept, and get the match acceptance created.
 *
 * `created_match_id` on the response is the handoff: it is why the client
 * never has to search for the game it just agreed to play. See
 * `useAcceptChallenge` for what happens with it.
 */
export function acceptChallenge(challengeId: string): Promise<Challenge> {
  return api.post<Challenge>(`/challenges/${challengeId}/accept`);
}

export function declineChallenge(challengeId: string): Promise<Challenge> {
  return api.post<Challenge>(`/challenges/${challengeId}/decline`);
}

/** Withdraws a challenge **the viewer sent**. Distinct from declining one. */
export function cancelChallenge(challengeId: string): Promise<void> {
  return api.delete<void>(`/challenges/${challengeId}`);
}
