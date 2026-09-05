import { useCallback, useEffect, useState } from "react";

/**
 * The console's theme preference — A64-027A §4.
 *
 * Three states rather than two, and the third is the one that matters: an
 * operator who has set their whole machine to dark at 3am has already
 * expressed a preference, and a console that shipped a hardcoded default
 * would override it. `system` defers; `light` and `dark` override.
 *
 * The choice is written to `data-theme` on the document element, which is
 * what `styles.css` keys its overrides off, and persisted per browser. It
 * is a display preference, so `localStorage` is the right home for it —
 * there is nothing here the server needs to know.
 */

const STORAGE_KEY = "arena64.admin.theme";

export const THEMES = ["system", "light", "dark"] as const;
export type Theme = (typeof THEMES)[number];

function isTheme(value: unknown): value is Theme {
  return typeof value === "string" && (THEMES as readonly string[]).includes(value);
}

function readStored(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (isTheme(stored)) return stored;
  } catch {
    /* Storage disabled. The console works; the preference does not persist. */
  }
  return "system";
}

/**
 * `system` removes the attribute entirely rather than writing a resolved
 * value. Writing `dark` when the machine is dark looks identical until the
 * machine changes, at which point a console that had resolved once would be
 * stuck in yesterday's theme.
 */
function apply(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function useTheme(): { theme: Theme; setTheme: (next: Theme) => void } {
  const [theme, setThemeState] = useState<Theme>(readStored);

  useEffect(() => {
    apply(theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* As above: the console still works, the choice just does not stick. */
    }
  }, []);

  return { theme, setTheme };
}
