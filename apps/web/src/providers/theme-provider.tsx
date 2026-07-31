"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";
import type { ComponentProps } from "react";

/**
 * Wraps `next-themes` rather than hand-rolling theme state: it injects a
 * blocking inline script before hydration that sets the `class` attribute
 * from `localStorage`/system preference, which is the only reliable way
 * to avoid a flash of the wrong theme — a `useEffect`-based approach
 * always paints light-then-dark once, visibly, on every reload.
 */
export function ThemeProvider({
  children,
  ...props
}: ComponentProps<typeof NextThemesProvider>) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      {...props}
    >
      {children}
    </NextThemesProvider>
  );
}
