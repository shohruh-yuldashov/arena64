/**
 * Every cache key this feature owns — A64-020.3 §15.
 *
 * ## Private and public never share a key
 *
 * `/profile/me` returns the account unfiltered; `/profiles/{username}`
 * returns it privacy-filtered. Cached under one key, a self-profile fetch
 * would populate what a public page later reads, and the public page would
 * render fields the viewer is not entitled to — the leak is silent, and it
 * only appears when the *same* person views their own public profile first.
 *
 * So `me` and `byUsername` are disjoint prefixes, and the auth layer's
 * `removeQueries()` on sign-out clears both along with everything else.
 *
 * ## Hierarchical, so invalidation can be precise
 *
 * `["profile"]` invalidates everything; `["profile", "public", name]` only
 * that player. After a privacy change, exactly the public profiles are
 * invalidated and the self profile — which privacy does not filter — is
 * left alone.
 */
export const profileKeys = {
  all: ["profile"] as const,

  /** The signed-in account. */
  me: () => [...profileKeys.all, "me"] as const,
  myRatings: () => [...profileKeys.all, "me", "ratings"] as const,
  preferences: () => [...profileKeys.all, "me", "preferences"] as const,
  privacy: () => [...profileKeys.all, "me", "privacy"] as const,

  /** Anybody's public profile, keyed by the name in the URL. */
  publicAll: () => [...profileKeys.all, "public"] as const,
  byUsername: (username: string) => [...profileKeys.publicAll(), username] as const,

  /** Per-player reads, keyed by id — the id a profile response carries. */
  ratings: (playerId: string) => [...profileKeys.all, "ratings", playerId] as const,
  tournaments: (playerId: string) => [...profileKeys.all, "tournaments", playerId] as const,
} as const;
