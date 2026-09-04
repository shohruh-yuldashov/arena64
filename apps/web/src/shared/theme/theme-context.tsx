import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";

import { ThemeContext, type ThemeContextValue } from "@/shared/theme/context";
import {
  isThemeMode,
  THEME_STORAGE_KEY,
  type ThemeMode,
  THEMES,
} from "@/shared/theme/theme-helpers";

/**
 * Light / dark / system, as a React context.
 *
 * ## Why this lives in `shared` and not in `app/providers`
 *
 * The provider is composed in `app/providers`, but the *hook* is consumed
 * by a widget (`widgets/theme-toggle`). A widget may not import `app`
 * (see `eslint.config.mjs`), so the context has to live below both. The
 * provider component sits beside it so there is one file to read.
 *
 * ## Why React Context rather than a store
 *
 * There is one value, it changes on a click, and everything that reads it
 * re-renders anyway because it is a theme. A store would add a dependency
 * and a subscription mechanism to solve a problem this does not have —
 * and the task rules out Zustand for exactly this class of state.
 *
 * ## Why the DOM is written in an effect but read at boot
 *
 * The first paint is handled by the inline script in `index.html`, which
 * runs before React exists — that is the only way to avoid a visible flash
 * of the wrong theme. This effect keeps the DOM in step *afterwards*. The
 * two agree because both read `THEME_STORAGE_KEY` and both toggle `.dark`;
 * `theme.test.tsx` asserts they still do.
 */
const DARK_QUERY = "(prefers-color-scheme: dark)";

function readStoredMode(): ThemeMode {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isThemeMode(stored) ? stored : "system";
  } catch {
    // Storage disabled (private mode, a locked-down browser). The app
    // works, the preference simply does not survive a reload.
    return "system";
  }
}

function prefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia(DARK_QUERY).matches;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(readStoredMode);
  const [systemIsDark, setSystemIsDark] = useState<boolean>(prefersDark);

  // The OS preference can change while the app is open — a scheduled
  // night shift, a manual toggle. Without this, `"system"` would mean
  // "the system, as it was when you loaded the page".
  useEffect(() => {
    const query = window.matchMedia(DARK_QUERY);
    const onChange = (event: MediaQueryListEvent) => setSystemIsDark(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const resolved: "light" | "dark" =
    mode === "system" ? (systemIsDark ? "dark" : "light") : mode;

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", resolved === "dark");
    // Tells the browser which scrollbars, form controls and default
    // backgrounds to draw. Without it a dark page keeps light native UI.
    root.style.colorScheme = resolved;
  }, [resolved]);

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      /* See `readStoredMode` — a session-only preference is the honest
         degradation, not a reason to fail the click. */
    }
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({ mode, resolved, setMode }),
    [mode, resolved, setMode],
  );

  return <ThemeContext value={value}>{children}</ThemeContext>;
}

/**
 * Throws outside a `ThemeProvider` rather than returning a default.
 *
 * A silent fallback would let a component render with the light theme
 * forever because somebody forgot a provider three files away — the exact
 * class of bug the reachability test in `app/App.test.tsx` exists to stop.
 */

// A64-025.13B §37. The context and its hook live in `context.ts`, which
// defines no component — see that module on why that matters at runtime.
export { type ThemeContextValue, useTheme } from "@/shared/theme/context";
export { THEME_STORAGE_KEY, type ThemeMode, THEMES };
