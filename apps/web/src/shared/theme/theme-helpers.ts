/**
 * The theme's vocabulary, in a module with no React in it.
 *
 * Separate from `theme-context.tsx` because **three** things need it and
 * only one of them is a component: the provider, the toggle widget, and
 * the pre-paint inline script in `index.html` — which cannot import
 * anything at all, and so agrees with this file by test
 * (`theme.test.tsx`) rather than by import.
 */

/** The `localStorage` key the chosen mode is persisted under.
 *
 * Declared here rather than inline so the two writers — the provider and
 * the pre-paint script — and any future "reset preferences" action have
 * one name to refer to. A drift between them presents as "it forgets my
 * setting sometimes" and nothing functional would catch it. */
export const THEME_STORAGE_KEY = "theme";

export const THEMES = ["light", "dark", "system"] as const;

export type ThemeMode = (typeof THEMES)[number];

/** Validates a value read from an untrusted source (localStorage, a URL
 * param) before trusting it as a `ThemeMode` — CLAUDE.md §9 rule 1,
 * validate at the boundary. */
export function isThemeMode(value: unknown): value is ThemeMode {
  return typeof value === "string" && (THEMES as readonly string[]).includes(value);
}
