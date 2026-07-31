import {
  QueryClient,
  defaultShouldDehydrateQuery,
  isServer,
} from "@tanstack/react-query";

/**
 * One `QueryClient` per server *request*, one per browser *tab* — never
 * one shared instance across both. A module-level singleton on the server
 * would leak cached data from one player's request into another's
 * response; App Router's mixed server/client rendering makes that mistake
 * easy to make silently. This is the pattern TanStack Query's own Next.js
 * App Router guide documents, reproduced here rather than linked, so the
 * reasoning travels with the code.
 */
function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Server state is stale-tolerant by nature (architecture.md
        // AD-22); zero would mean every render refetches, defeating the
        // cache. A minute is a safe, uncommitted-to default — real
        // per-query staleTime belongs with the query that knows its own
        // volatility, once one exists.
        staleTime: 60 * 1000,
      },
      dehydrate: {
        // Also dehydrate queries still in flight, not just settled ones —
        // required for React 19's Suspense-driven streaming SSR to hand
        // off a pending query to the client instead of restarting it.
        shouldDehydrateQuery: (query) =>
          defaultShouldDehydrateQuery(query) || query.state.status === "pending",
      },
    },
  });
}

let browserQueryClient: QueryClient | undefined;

export function getQueryClient(): QueryClient {
  if (isServer) {
    return makeQueryClient();
  }

  browserQueryClient ??= makeQueryClient();
  return browserQueryClient;
}
