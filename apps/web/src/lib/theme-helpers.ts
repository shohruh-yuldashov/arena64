/**
 * Theme constants and guards shared by `providers/theme-provider.tsx` and
 * `components/theme-toggle.tsx`. Split out rather than left implicit in
 * `next-themes`' defaults so the storage key and the valid mode set are
 * declared once, explicitly (CLAUDE.md §2.1 — "explicit over implicit"),
 * instead of relying on a library default a reader has to go look up.
 */

/** The `localStorage` key `next-themes` persists the chosen mode under.
 * Passed explicitly to `ThemeProvider` (rather than left as the library's
 * implicit default) so any other code that needs to read or clear it —
 * a future "reset preferences" action — has one documented name to use. */
export const THEME_STORAGE_KEY = "theme";

export const THEMES = ["light", "dark", "system"] as const;

export type ThemeMode = (typeof THEMES)[number];

/** Validates a value read from an untrusted source (localStorage, a URL
 * param) before trusting it as a `ThemeMode` — CLAUDE.md §9 rule 1,
 * validate at the boundary. */
export function isThemeMode(value: unknown): value is ThemeMode {
  return typeof value === "string" && (THEMES as readonly string[]).includes(value);
}
