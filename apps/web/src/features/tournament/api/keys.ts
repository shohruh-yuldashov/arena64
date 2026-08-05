import type { TournamentFilters } from "@/features/tournament/api";

/**
 * Every cache key the tournament surfaces own — A64-020.6 §4.
 *
 * ## Four keys because the backend has four surfaces
 *
 * A tournament's detail, its bracket, its standings and the viewer's own
 * entry are four endpoints with four lifetimes: a completed tournament's
 * standings never change again, its bracket changes only while it is being
 * played, and a registration changes the moment the viewer presses a
 * button. Cached as one object they would share the shortest of those
 * lifetimes, so entering a tournament would re-fetch a 127-node bracket
 * that could not possibly have moved (§4).
 *
 * ## The cursor is not in the key
 *
 * `useInfiniteQuery` owns the page chain under `list(filters)`. A key per
 * cursor would make "load more" a new cache entry each time and leave the
 * earlier pages to be garbage-collected out from under the list — and it
 * would require decoding the cursor to build the key, which is the one
 * thing an opaque cursor exists to prevent.
 *
 * ## Filters *are* in the key
 *
 * Two filter combinations are two different lists, and serving one under
 * the other's key is how a "registration open" view ends up showing
 * yesterday's completed tournaments. Serialised through a stable field
 * order, so `{status, rated}` and `{rated, status}` are one entry.
 *
 * ## What is deliberately absent
 *
 * A player's tournament history. `GET /players/{id}/tournaments` already
 * has a key — `profileKeys.tournaments(playerId)` — and the profile's
 * `TournamentHistory` widget already reads it. A second key over one
 * endpoint is two caches that disagree after a registration (§20).
 */
export const tournamentKeys = {
  root: ["tournament"] as const,

  lists: () => [...tournamentKeys.root, "list"] as const,
  list: (filters: TournamentFilters) =>
    [...tournamentKeys.lists(), serialise(filters)] as const,

  detail: (tournamentId: string) => [...tournamentKeys.root, "detail", tournamentId] as const,
  bracket: (tournamentId: string) => [...tournamentKeys.root, "bracket", tournamentId] as const,
  standings: (tournamentId: string) =>
    [...tournamentKeys.root, "standings", tournamentId] as const,

  /** The viewer's own entry. Never another player's — the route has no id. */
  myRegistration: (tournamentId: string) =>
    [...tournamentKeys.root, "registration", "me", tournamentId] as const,
} as const;

/**
 * Filters as one stable string.
 *
 * Sorted by key, so the entry does not depend on the order a caller
 * happened to write the object literal in; absent and `undefined` collapse
 * to the same thing, because "no status filter" is one query however it was
 * spelled.
 */
function serialise(filters: TournamentFilters): string {
  const entries = Object.entries(filters)
    .filter(([, value]) => value !== undefined && value !== null)
    .sort(([a], [b]) => a.localeCompare(b));
  return entries.map(([key, value]) => `${key}=${String(value)}`).join("&");
}
