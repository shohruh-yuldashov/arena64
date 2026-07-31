"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";
import type { ComponentProps } from "react";

import { THEME_STORAGE_KEY, THEMES } from "@/lib/theme-helpers";

/**
 * Wraps `next-themes` rather than hand-rolling theme state: it injects a
 * blocking inline script before hydration that sets the `class` attribute
 * from `localStorage`/system preference, which is the only reliable way
 * to avoid a flash of the wrong theme — a `useEffect`-based approach
 * always paints light-then-dark once, visibly, on every reload.
 *
 * `storageKey` and `themes` are passed explicitly from `lib/theme-helpers.ts`
 * rather than left as next-themes' implicit defaults, so both are declared
 * in exactly one place a reader can find without opening this library's
 * source.
 */
export function ThemeProvider({
  children,
  ...props
}: ComponentProps<typeof NextThemesProvider>) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="system"
      themes={[...THEMES]}
      storageKey={THEME_STORAGE_KEY}
      enableSystem
      disableTransitionOnChange
      {...props}
    >
      {children}
    </NextThemesProvider>
  );
}
