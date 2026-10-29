import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * The leaderboard with names on it — A64-033.
 *
 * Two reads, both the server's composition: who is on a ladder, and in
 * what order, is decided there — a hidden country, a restricted account and
 * a frozen rating never reach this client to be filtered out.
 */
type Schemas = components["schemas"];

export type LeaderboardPage = Schemas["LeaderboardPlayersResponse"];
export type LeaderboardPlayer = Schemas["LeaderboardPlayerResponse"];
export type LeaderboardStanding = Schemas["LeaderboardStandingResponse"];

export interface LeaderboardFilter {
  speedClass: string;
  /** ISO 3166-1 alpha-2, or `null` for every country. */
  country: string | null;
}

function query(filter: LeaderboardFilter, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams({ speed_class: filter.speedClass, ...extra });
  if (filter.country !== null) params.set("country", filter.country);
  return params.toString();
}

export function readLeaderboard(
  filter: LeaderboardFilter,
  options: { after?: string | null; limit: number },
): Promise<LeaderboardPage> {
  // The cursor is opaque and sent back verbatim: it carries the ranks the
  // walk has counted so far, and decoding it here would couple the client
  // to that.
  const extra: Record<string, string> = { limit: String(options.limit) };
  if (options.after) extra.after = options.after;
  return api.get<LeaderboardPage>(`/leaderboard/players?${query(filter, extra)}`);
}

export function readMyStanding(filter: LeaderboardFilter): Promise<LeaderboardStanding> {
  return api.get<LeaderboardStanding>(`/leaderboard/players/me?${query(filter)}`);
}
