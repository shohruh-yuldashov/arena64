/**
 * The query key factory — one consistent, hierarchical key shape for every
 * future TanStack Query hook, so cache invalidation can target exactly the
 * queries it means to ("every match list") without also invalidating
 * queries it doesn't ("one specific match's detail").
 *
 * No entity keys are defined here — no feature exists yet (this task's
 * scope is platform infrastructure only). A future feature does:
 *
 *     export const matchKeys = createQueryKeys("matches");
 *     matchKeys.all              // ["matches"]
 *     matchKeys.lists()          // ["matches", "list"]
 *     matchKeys.list({ status }) // ["matches", "list", { status }]
 *     matchKeys.details()        // ["matches", "detail"]
 *     matchKeys.detail(id)       // ["matches", "detail", id]
 *
 * and TanStack Query's `invalidateQueries({ queryKey: matchKeys.lists() })`
 * invalidates every list variant without touching cached details, because
 * key arrays are matched by prefix.
 */

export interface QueryKeyFactory<TScope extends string> {
  all: readonly [TScope];
  lists: () => readonly [TScope, "list"];
  list: (
    filters?: Record<string, unknown>,
  ) => readonly [TScope, "list", Record<string, unknown> | undefined];
  details: () => readonly [TScope, "detail"];
  detail: (id: string) => readonly [TScope, "detail", string];
}

export function createQueryKeys<TScope extends string>(scope: TScope): QueryKeyFactory<TScope> {
  return {
    all: [scope] as const,
    lists: () => [scope, "list"] as const,
    list: (filters) => [scope, "list", filters] as const,
    details: () => [scope, "detail"] as const,
    detail: (id) => [scope, "detail", id] as const,
  };
}
