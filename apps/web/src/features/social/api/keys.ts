/**
 * Every cache key the social feature owns — A64-020.4 §16.
 *
 * ## The invalidation matrix
 *
 * Each mutation names exactly the keys its write can have changed. A
 * blanket `invalidateQueries()` would refetch a tournament history because
 * somebody accepted a friend request.
 *
 * | Mutation | Invalidates |
 * | --- | --- |
 * | send request | `search`, `outgoing`, `counts`, the target's public profile |
 * | cancel request | `search`, `outgoing`, `counts`, public profile |
 * | accept | `search`, `incoming`, `friends`, `counts`, public profile |
 * | decline | `search`, `incoming`, `counts`, public profile |
 * | remove friend | `search`, `friends`, `counts`, public profile |
 * | block | **everything social** + public profile — a block ends a friendship, declines pending requests and removes the target from lists, so every social view is stale at once |
 * | unblock | `blocked`, `search`, public profile |
 *
 * `search` is invalidated by **prefix**, not by query string: a mutation
 * changes the relationship on whichever result page happens to be cached,
 * and the client does not know which term produced it.
 *
 * Public profiles are `profileKeys.publicAll()` — the profile feature's,
 * imported rather than redeclared, because a second spelling of that key
 * is a cache that only half invalidates.
 */
export const socialKeys = {
  all: ["social"] as const,

  /** Search, keyed by the **normalised** term — see `useUserSearch`. */
  searches: () => [...socialKeys.all, "search"] as const,
  search: (query: string) => [...socialKeys.searches(), query] as const,

  friends: () => [...socialKeys.all, "friends"] as const,
  friendCount: () => [...socialKeys.all, "friends", "count"] as const,

  requests: () => [...socialKeys.all, "requests"] as const,
  incoming: () => [...socialKeys.requests(), "incoming"] as const,
  outgoing: () => [...socialKeys.requests(), "outgoing"] as const,

  blocked: () => [...socialKeys.all, "blocked"] as const,
} as const;
