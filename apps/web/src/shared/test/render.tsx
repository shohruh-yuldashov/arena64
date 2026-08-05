import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";

import { App } from "@/app/App";
import { AppProviders } from "@/app/providers";
import { createAppRouter } from "@/app/router";
import type { AuthChannel } from "@/features/auth/model/auth-channel";

/**
 * Two ways to mount something, and the difference matters.
 *
 * `renderApp` mounts the **real** `App` — the whole provider graph, the
 * real router, the real shell. That is what a reachability test needs:
 * asserting against a hand-assembled tree would prove the tree the test
 * built works, not the one that ships.
 *
 * `renderWithProviders` mounts one component inside the same providers,
 * for tests about that component rather than about the app.
 *
 * Both build a fresh `QueryClient` with **retries off**. A test that
 * exercises a failure would otherwise wait through the production backoff
 * (1s, then 2s) before asserting, and a suite that sleeps is a suite
 * nobody runs.
 */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

export function renderApp(
  options: { path?: string; channel?: AuthChannel } = {},
): RenderResult & { queryClient: QueryClient } {
  const router = createAppRouter(
    createMemoryHistory({ initialEntries: [options.path ?? "/"] }),
  );
  const queryClient = createTestQueryClient();
  return {
    ...render(
      <App
        router={router}
        queryClient={queryClient}
        {...(options.channel ? { authChannel: options.channel } : {})}
      />,
    ),
    queryClient,
  };
}

export function renderWithProviders(ui: ReactElement): RenderResult & {
  queryClient: QueryClient;
} {
  const queryClient = createTestQueryClient();
  return {
    ...render(<AppProviders queryClient={queryClient}>{ui}</AppProviders>),
    queryClient,
  };
}
