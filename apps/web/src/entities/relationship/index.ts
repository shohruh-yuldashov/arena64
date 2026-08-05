import type { components } from "@/shared/api/generated/schema";

/**
 * What the viewer may do about a player — A64-020.4.
 *
 * An alias over the **generated** enum, never a re-declaration: the states
 * are the backend's and a hand-written copy would drift the day a sixth
 * arrives.
 *
 * ## `null` is not `none`
 *
 * The API omits `relationship` for an anonymous reader and on the reader's
 * own profile — there is nobody to have a relationship with, and nobody is
 * their own friend. `none` means *signed in, no relationship*, which is
 * what an "Add friend" control renders from. So the type is
 * `RelationshipState | null | undefined` everywhere and `actionsFor`
 * returns an empty list for the absent case rather than treating it as
 * `none`.
 *
 * ## `blocked` is one-directional
 *
 * It means **the viewer blocked this player**. There is no state for
 * blocked-by-target and the client must never infer one: a player who
 * could tell they had been blocked would have exactly what a block
 * withholds.
 */
export type RelationshipState = components["schemas"]["RelationshipState"];

/**
 * Every action a relationship permits — the single source of truth for
 * what a button set may contain.
 *
 * Derived from **one** state rather than assembled from several booleans,
 * which is what makes the impossible combinations unrepresentable. There is
 * no arrangement of inputs that yields "Add friend" beside "Accept", or
 * "Friend" beside "Blocked", because the input is one closed value.
 *
 * Used by search rows, the friends list, both request lists and the public
 * profile — so a transition is written once and cannot be got right in one
 * place and wrong in another.
 */
export type RelationshipAction =
  | "send_request"
  | "cancel_request"
  | "accept_request"
  | "decline_request"
  | "remove_friend"
  | "block"
  | "unblock";

const ACTIONS: Record<RelationshipState, readonly RelationshipAction[]> = {
  none: ["send_request", "block"],
  // The viewer sent it, so it is theirs to withdraw. **Not** "decline" —
  // that is the other party's word for the same row, and offering it here
  // would be the client disagreeing with itself about who acted.
  outgoing_request: ["cancel_request", "block"],
  incoming_request: ["accept_request", "decline_request", "block"],
  friend: ["remove_friend", "block"],
  // Nothing else. A blocked player must not be friendable, requestable or
  // removable — the block is the only relationship there is, and lifting
  // it is the only move.
  blocked: ["unblock"],
};

/**
 * The actions to offer, or none at all.
 *
 * `[]` for `null`/`undefined` — an anonymous viewer and the viewer's own
 * profile both land here, and both must render no social controls.
 */
export function actionsFor(
  state: RelationshipState | null | undefined,
): readonly RelationshipAction[] {
  return state == null ? [] : ACTIONS[state];
}

/** Whether this action ends something and should be confirmed first. */
export function isDestructive(action: RelationshipAction): boolean {
  return action === "remove_friend" || action === "block";
}
