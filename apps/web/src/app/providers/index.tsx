import type { QueryClient } from "@tanstack/react-query";
import { QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import UnexpectedErrorPage from "@/pages/unexpected-error";
import { createQueryClient } from "@/shared/api";
import { ThemeProvider } from "@/shared/theme/theme-context";
import { ErrorBoundary } from "@/shared/ui";

/**
 * The provider graph, composed once.
 *
 *     ErrorBoundary          catches everything below, including the router
 *       └─ ThemeProvider     the error page must be themed too
 *            └─ QueryClientProvider
 *                 └─ children (the router, then a layout, then a page)
 *
 * ## Why this order and no other
 *
 * **`ErrorBoundary` is outermost** because a boundary cannot catch a throw
 * from a component above it. Inside the router, it would leave a router
 * failure to render as a blank document — which is precisely the failure a
 * boundary exists for.
 *
 * **`ThemeProvider` is above `ErrorBoundary`'s fallback content** — the
 * fallback is a page, and a page rendered outside the theme is a white
 * flash in a dark session at the worst possible moment. It is nested
 * *inside* the boundary rather than outside so that a throw in the theme
 * effect is still caught.
 *
 * **`QueryClientProvider` is innermost** because nothing above it issues a
 * query. Hoisting it higher would only widen what a cache failure can take
 * down.
 *
 * ## One client, created once
 *
 * `useState(createQueryClient)` — the initialiser form, so the factory runs
 * on the first render only. Calling `createQueryClient()` inline would mint
 * a new cache on every render and quietly disable caching altogether; the
 * app would work, slowly, and nothing would say why.
 *
 * `queryClient` is injectable so a test can supply one with retries off.
 */
export function AppProviders({
  children,
  queryClient,
}: {
  children: ReactNode;
  queryClient?: QueryClient;
}) {
  const [fallbackClient] = useState(createQueryClient);
  const client = queryClient ?? fallbackClient;

  return (
    <ErrorBoundary
      scope="app-root"
      fallback={({ reset }) => (
        <ThemeProvider>
          <UnexpectedErrorPage reset={reset} />
        </ThemeProvider>
      )}
    >
      <ThemeProvider>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}
