import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * The replay read — A64-020.5E §1.
 *
 * One call, one generated type. The response is the **authoritative
 * archive**: every ply carries the full board it produced, so nothing here
 * replays anything and the client holds no second engine (§8).
 *
 * `404` is deliberately **not** translated to `null`. Unlike the lobby's
 * reads, where "you are not queued" is an ordinary state, a replay that
 * cannot be found is a screen of its own — and the backend gives the same
 * `404` for a match that does not exist and one the viewer may not see, so
 * flattening it would discard the only signal the UI has.
 */
type Schemas = components["schemas"];

export type MatchReplay = Schemas["MatchReplayResponse"];
export type ReplayPly = Schemas["ReplayPlyResponse"];
export type ReplaySeat = Schemas["ReplaySeatResponse"];
export type PlacedPiece = Schemas["PlacedPieceResponse"];

export function readReplay(matchId: string): Promise<MatchReplay> {
  return api.get<MatchReplay>(`/matches/${matchId}/replay`);
}
