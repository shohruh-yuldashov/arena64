import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * A player's finished matches — A64-020.5F §14.
 *
 * One call. The opponent and the time control arrive **composed** in the
 * response, so a list row needs no second request — §17 forbids a profile
 * lookup per row and the backend batches it instead.
 */
type Schemas = components["schemas"];

export type MatchHistoryPage = Schemas["MatchHistoryResponse"];
export type MatchHistoryEntry = Schemas["MatchHistoryEntryResponse"];

export function readMatchHistory(
  playerId: string,
  options: { after?: string | null; limit?: number } = {},
): Promise<MatchHistoryPage> {
  const query = new URLSearchParams();
  // **The cursor is opaque and is sent back verbatim** — §16. Decoding it
  // would couple this client to a base64 encoding of two server-side
  // columns, which is exactly what an opaque cursor exists to prevent.
  if (options.after) query.set("after", options.after);
  if (options.limit) query.set("limit", String(options.limit));

  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return api.get<MatchHistoryPage>(`/players/${playerId}/matches${suffix}`);
}
