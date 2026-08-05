/**
 * Every cache key the match history owns — A64-020.5F §16.
 *
 * Keyed by **player**, not by "me": the endpoint takes a player id and the
 * viewer is the access token, so two players' histories are two entries and
 * signing in as somebody else cannot be served the previous account's.
 *
 * The cursor is *not* in the key. `useInfiniteQuery` owns the page chain
 * under one entry, which is the whole reason to use it: a key per cursor
 * would make "load more" a new cache entry each time and leave the earlier
 * pages to be garbage-collected out from under the list.
 */
export const matchHistoryKeys = {
  root: ["game", "match-history"] as const,
  player: (playerId: string) => ["game", "match-history", playerId] as const,
};
