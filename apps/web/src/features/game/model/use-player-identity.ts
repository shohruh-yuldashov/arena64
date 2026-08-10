import { useQueries } from "@tanstack/react-query";

import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * A player id turned into a name and a picture — A64-025.6 §5.
 *
 * ## The endpoint exists for exactly this
 *
 * `GET /api/v1/users/{user_id}` is documented in the schema as existing
 * "because every cross-context reference on this platform is an opaque
 * `player_id` (DM-06), so a match card or a leaderboard row holds an id and
 * needs a handle to render". The game room holds two ids and needs two
 * handles; this is the read that was built for it, and no contract had to
 * change.
 *
 * It **carries no email, no account state and no storage key** — that was
 * removed in A64-012.6 — so rendering it beside a board leaks nothing.
 *
 * ## What it deliberately does not fetch
 *
 * Ratings — and since A64-025.6B it does not need to. They arrive on the
 * snapshot as the seat values the match was created with, which costs no
 * request at all. Reading them from `GET /profiles/{username}` would have
 * meant a second privacy-governed round trip per player during a bullet
 * game, and would have answered a different question: what the player
 * rates *now*, rather than what they rated when they sat down.
 *
 * ## One query per player, cached across matches
 *
 * `useQueries` rather than two hooks, so the number of players is data
 * rather than structure. An identity does not change during a game, so
 * `staleTime` is long: re-reading it on every reconnect would be a request
 * per socket blip for a name that cannot have moved.
 */
export type PlayerIdentity = components["schemas"]["PublicUserResponse"];

/** Long, because a display name does not change mid-match. */
const IDENTITY_STALE_MS = 5 * 60_000;

export function usePlayerIdentities(
  ids: readonly (string | null)[],
): Map<string, PlayerIdentity> {
  const wanted = [...new Set(ids.filter((id): id is string => id !== null))];

  const results = useQueries({
    queries: wanted.map((id) => ({
      queryKey: ["player-identity", id] as const,
      queryFn: async (): Promise<PlayerIdentity> => {
        return api.get<PlayerIdentity>(`/users/${encodeURIComponent(id)}`);
      },
      staleTime: IDENTITY_STALE_MS,
      // A name that will not load is not worth three attempts during a
      // game; the seat falls back to a neutral label and the board plays on.
      retry: false,
    })),
  });

  const identities = new Map<string, PlayerIdentity>();
  results.forEach((result, index) => {
    const id = wanted[index];
    if (id !== undefined && result.data !== undefined) identities.set(id, result.data);
  });
  return identities;
}
