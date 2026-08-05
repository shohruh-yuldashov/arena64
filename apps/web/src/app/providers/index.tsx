import type { QueryClient } from "@tanstack/react-query";
import { QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import type { AuthChannel } from "@/features/auth/model/auth-channel";
import { SessionProvider } from "@/features/auth/model/session-provider";
import UnexpectedErrorPage from "@/pages/unexpected-error";
import { createQueryClient } from "@/shared/api";
import { I18nProvider } from "@/shared/i18n";
import { ThemeProvider } from "@/shared/theme/theme-context";
import { ErrorBoundary } from "@/shared/ui";

/**
 * The provider graph, composed once.
 *
 *     ErrorBoundary            catches everything below, including the router
 *       └─ ThemeProvider       the error page must be themed too
 *            └─ I18nProvider   ...and translated
 *                 └─ QueryClientProvider
 *                      └─ SessionProvider
 *                           └─ children (router → layout → page)
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
 * **`QueryClientProvider` is above `SessionProvider`** and not below it,
 * because signing out has to clear the cache: every query was fetched *as
 * somebody*, and leaving it would show the previous user's data to whoever
 * signs in next on the device. `SessionProvider` calls `useQueryClient`, so
 * it has to be inside.
 *
 * **`SessionProvider` is innermost** because everything below it — the
 * router, the guards, every page — reads the session, and nothing above it
 * does.
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
  authChannel,
}: {
  children: ReactNode;
  queryClient?: QueryClient;
  authChannel?: AuthChannel;
}) {
  const [fallbackClient] = useState(createQueryClient);
  const client = queryClient ?? fallbackClient;

  return (
    <ErrorBoundary
      scope="app-root"
      fallback={({ reset }) => (
        <ThemeProvider>
          <I18nProvider>
            <UnexpectedErrorPage reset={reset} />
          </I18nProvider>
        </ThemeProvider>
      )}
    >
      <ThemeProvider>
        <I18nProvider>
          <QueryClientProvider client={client}>
            <SessionProvider {...(authChannel ? { channel: authChannel } : {})}>
              {children}
            </SessionProvider>
          </QueryClientProvider>
        </I18nProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}
