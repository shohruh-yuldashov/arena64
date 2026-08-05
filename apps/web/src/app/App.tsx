import type { QueryClient } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { useState } from "react";

import { AppProviders } from "@/app/providers";
import { type AppRouter, createAppRouter } from "@/app/router";
import type { AuthChannel } from "@/features/auth/model/auth-channel";
import type { RealtimeClient } from "@/shared/realtime";

/**
 * The composition root.
 *
 *     App
 *      └─ AppProviders   ErrorBoundary → ThemeProvider → QueryClientProvider
 *           └─ RouterProvider
 *                └─ AppShell   (the root route's component)
 *                     └─ Page
 *
 * Everything the running application wires together is named in this file
 * or in `app/providers`, and nowhere else — which is what makes
 * `App.test.tsx`'s reachability assertion possible: a provider that is not
 * mentioned here is not in the app, however carefully it was written.
 *
 * Both dependencies are injectable and both default to the real thing.
 * That is not a testing hook bolted on: a router owns a history and a
 * query client owns a cache, and a test that shared either with another
 * test would pass or fail by file order.
 */
export function App({
  router,
  queryClient,
  authChannel,
  realtimeClient,
}: {
  router?: AppRouter;
  queryClient?: QueryClient;
  authChannel?: AuthChannel;
  realtimeClient?: RealtimeClient;
} = {}) {
  // The initialiser form, so the default router is built once rather than
  // on every render — a fresh history per render would reset navigation.
  const [fallbackRouter] = useState(createAppRouter);

  return (
    <AppProviders
      {...(queryClient ? { queryClient } : {})}
      {...(authChannel ? { authChannel } : {})}
      {...(realtimeClient ? { realtimeClient } : {})}
    >
      <RouterProvider router={router ?? fallbackRouter} />
    </AppProviders>
  );
}
