import { createContext, use } from "react";

import type { ThemeMode } from "@/shared/theme/theme-helpers";

/**
 * The theme context, and the hook that reads it — A64-025.13B §37.
 *
 * Split from the provider for the reason `shared/i18n/context` records: a
 * module that creates a context **and** exports a component can be
 * hot-swapped by Fast Refresh, and the swap gives every already-mounted
 * consumer a context object the new provider is not filling.
 *
 * Nothing here is a component, so Fast Refresh will not swap this module —
 * it triggers a full reload instead, which is the safe failure.
 */
export interface ThemeContextValue {
  /** What the user chose: `"light"`, `"dark"` or `"system"`. */
  mode: ThemeMode;
  /** What that currently *means* — `"system"` resolved against the OS. */
  resolved: "light" | "dark";
  setMode: (mode: ThemeMode) => void;
}

export const ThemeContext = createContext<ThemeContextValue | null>(null);

export function useTheme(): ThemeContextValue {
  const value = use(ThemeContext);
  if (value === null) {
    throw new Error("useTheme must be used inside a ThemeProvider.");
  }
  return value;
}
