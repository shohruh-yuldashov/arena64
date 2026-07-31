"use client";

import type { ReactNode } from "react";

import { AuthProvider } from "@/providers/auth-provider";
import { QueryProvider } from "@/providers/query-provider";
import { ThemeProvider } from "@/providers/theme-provider";

/**
 * The composition root for every provider that must run on the client.
 * `LocaleProvider` (next-intl) deliberately stays outside this boundary —
 * see its own docstring — so this is Theme, Query, and Auth, nested in the
 * order least- to most-dependent: theme depends on nothing here; query
 * depends on nothing here yet, but will eventually read auth state to key
 * or gate requests, which is why it wraps `AuthProvider` rather than the
 * reverse.
 */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider>
      <QueryProvider>
        <AuthProvider>{children}</AuthProvider>
      </QueryProvider>
    </ThemeProvider>
  );
}
