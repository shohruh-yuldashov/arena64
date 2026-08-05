import type {
  Bracket,
  BracketAttempt,
  BracketNode,
  TournamentParticipant,
} from "@/features/tournament/api";

/**
 * What a bracket node *is*, derived from what the backend published —
 * A64-020.6 §12, §13.
 *
 * ## Every input is a durable server field
 *
 * `advancement_reason`, `winner_id`, the two seats and each attempt's
 * `status` are all facts the tournament recorded. Nothing here infers a
 * rule: this decides which of five shapes to *render*, not who advanced.
 * §8 is explicit that the backend stays authoritative, and the moment this
 * file starts deciding eliminations it has stopped being a view model.
 *
 * ## Why five states and not three
 *
 * The two that are usually collapsed are the two worth keeping apart:
 *
 *     bye      one player advanced because there was nobody to play. It is
 *              a real, decided node with a winner
 *     pending  a seat is empty because the match beneath has not finished.
 *              It is *waiting*, and it will become a real pairing
 *
 * Both show one name and one blank. Collapsing them renders a semi-final
 * in progress as "X received a bye", which is the exact confusion the
 * backend's own `is_bye` had until this phase fixed it — see the
 * `fix(tournament)` commit. §13 requires them distinguishable, so they are
 * separate members rather than a boolean plus a comment.
 *
 * `ready` is separate from `live` for the same kind of reason: a pairing
 * whose players are both known but whose match has not been created yet
 * has nothing to link to, and offering a dead link is worse than offering
 * none.
 */
export type NodeState = "bye" | "completed" | "live" | "ready" | "pending";

/** An attempt still being played — the backend's `created`, not `completed`. */
function isLive(attempt: BracketAttempt): boolean {
  return attempt.status === "created";
}

export function nodeStateOf(node: BracketNode): NodeState {
  // A bye first, because it is also "completed" and the specific answer is
  // the useful one. Read off `advancement_reason`, which is the durable
  // record of *why* somebody advanced — never off a seat count, which
  // cannot tell a bye from a node still waiting for its opponent.
  if (node.advancement_reason === "bye") return "bye";
  if (node.winner_id != null) return "completed";
  if (node.attempts.some(isLive)) return "live";

  const seated = [node.light_player_id, node.dark_player_id].filter((id) => id != null);
  return seated.length === 2 ? "ready" : "pending";
}

/**
 * The match a live node links to, or `null`.
 *
 * The **live attempt**, not the last one: a node that drew and was replayed
 * has two attempts, and the one being played is the one a spectator wants.
 */
export function liveMatchOf(node: BracketNode): string | null {
  return node.attempts.find(isLive)?.match_id ?? null;
}

/**
 * Every finished match this node produced, oldest first.
 *
 * Plural because a drawn pairing is replayed (the bounded rematch policy),
 * so a node can own two or three games and each is a real replay. A bye
 * produces none, which is why `is_bye` and "has no attempts" are not the
 * same test — an adjudicated node has attempts too.
 */
export function replayableMatchesOf(node: BracketNode): BracketAttempt[] {
  return node.attempts
    .filter((attempt) => !isLive(attempt))
    .toSorted((a, b) => a.attempt_number - b.attempt_number);
}

/**
 * Participants by id, for turning a seat into a name.
 *
 * A `Map` built once per bracket rather than a `find` per seat: a 128-player
 * field is 127 nodes and 254 seats, and a linear scan per seat is 32,000
 * comparisons for a list the server already sent whole.
 */
export function participantsById(
  participants: TournamentParticipant[],
): Map<string, TournamentParticipant> {
  return new Map(participants.map((participant) => [participant.player_id, participant]));
}

/**
 * Which round is the final — the one with a single node.
 *
 * Derived from the shape rather than from a count, because a bracket read
 * before materialisation has no rounds at all and `rounds.length` would
 * name a final that does not exist.
 */
export function finalRoundNumber(bracket: Bracket): number | null {
  const last = bracket.rounds.at(-1);
  return last !== undefined && last.nodes.length === 1 ? last.round_number : null;
}

/**
 * Whether this tournament is still moving, and therefore worth re-reading.
 *
 * The **only** input to the polling decision (§19). A completed or
 * cancelled tournament is finished for good — its bracket is immutable and
 * its standings were materialised once — so polling one is a request per
 * interval that can never return anything new.
 */
export function isMoving(status: string): boolean {
  return status === "in_progress" || status === "registration_closed";
}
