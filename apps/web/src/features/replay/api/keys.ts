/**
 * Every cache key the replay owns — A64-020.5E §4.
 *
 * One key, because a replay is one immutable document. There is no
 * pagination, no partial read and nothing a mutation can invalidate: a
 * finished match's log does not change, and the engine version that would
 * refuse it does not change either.
 *
 * **Keyed by match id, and that is a privacy mechanism as well as a cache
 * one** (§17). Two replays never share an entry, so a viewer who signs out
 * and back in as somebody else cannot be served the previous account's
 * match from cache — the key would have to collide, and it cannot.
 */
export const replayKeys = {
  root: ["game", "replay"] as const,
  byMatch: (matchId: string) => ["game", "replay", matchId] as const,
};
