import type { QueryClient } from "@tanstack/react-query";
import { QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import { RealtimeProvider } from "@/app/providers/realtime-provider";
import type { AuthChannel } from "@/features/auth/model/auth-channel";
import { SessionProvider } from "@/features/auth/model/session-provider";
import UnexpectedErrorPage from "@/pages/unexpected-error";
import { createQueryClient } from "@/shared/api";
import { I18nProvider } from "@/shared/i18n";
import type { RealtimeClient } from "@/shared/realtime";
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
 *                           └─ RealtimeProvider
 *                                └─ children (router → layout → page)
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
 * **`SessionProvider` is above `RealtimeProvider`** because the socket is
 * authenticated: it cannot start until there is a session, and signing out
 * must close it. That dependency is one-directional, which is why the
 * session provider knows nothing about a socket and publishes
 * `onSessionEnded` instead — A64-020.5B §3.
 *
 * **`RealtimeProvider` is innermost, and above the router** — A64-020.5B.
 * One socket per tab (AD-11), and it survives navigation: a connection
 * owned by the game page would be rebuilt every time a player glanced at
 * their profile mid-game, and each rebuild costs a ticket, a handshake and
 * a full resume. Its context value is referentially stable forever, so
 * mounting it here re-renders nothing.
 *
 * ## One client, created once
 *
 * `useState(createQueryClient)` — the initialiser form, so the factory runs
 * on the first render only. Calling `createQueryClient()` inline would mint
 * a new cache on every render and quietly disable caching altogether; the
 * app would work, slowly, and nothing would say why.
 *
 * `queryClient` is injectable so a test can supply one with retries off,
 * and `realtimeClient` for the same reason: a test that wants to observe
 * the socket needs the instance the app actually uses, not a second one.
 */
export function AppProviders({
  children,
  queryClient,
  authChannel,
  realtimeClient,
}: {
  children: ReactNode;
  queryClient?: QueryClient;
  authChannel?: AuthChannel;
  /** Injectable for the same reason `queryClient` is — see below. */
  realtimeClient?: RealtimeClient;
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
              <RealtimeProvider {...(realtimeClient ? { client: realtimeClient } : {})}>
                {children}
              </RealtimeProvider>
            </SessionProvider>
          </QueryClientProvider>
        </I18nProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}
