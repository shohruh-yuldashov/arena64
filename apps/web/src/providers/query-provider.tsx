"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import type { ReactNode } from "react";

import { getQueryClient } from "@/lib/query-client";

/**
 * Server state — architecture.md AD-22's first of three state categories,
 * kept deliberately separate from Zustand's client/UI state (`stores/`)
 * and next-intl's locale state. `getQueryClient()` (not `new QueryClient()`
 * here directly) is what makes this safe under the App Router's mixed
 * server/client rendering — see its own docstring.
 */
export function QueryProvider({ children }: { children: ReactNode }) {
  const queryClient = getQueryClient();

  return (
    <QueryClientProvider client={queryClient}>
      {children}
      {process.env.NODE_ENV === "development" && (
        <ReactQueryDevtools initialIsOpen={false} />
      )}
    </QueryClientProvider>
  );
}
