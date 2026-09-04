import { createContext, use } from "react";

// Types only, so the cycle back to `index.tsx` is erased at build time
// and no module here depends on one there at runtime.
import type { Locale, TranslationKey } from "@/shared/i18n";

/**
 * The translation context, and the hook that reads it — A64-025.13B §37.
 *
 * ## Why this is not in `index.tsx` with the provider
 *
 * A module that creates a context **and** exports a component is
 * hot-swappable by React Fast Refresh. When it is swapped, `createContext`
 * runs again and produces a **new context object** — while every component
 * already mounted still holds the old one. The provider is rendering; the
 * consumer sees `null`; and `useTranslation` throws
 * "must be used inside an I18nProvider" under a tree that plainly has one.
 *
 * That is the exact fault A64-025.12A §33.3 recorded and could not
 * reproduce, so this is a **precaution on a theory, not a proven fix** — and
 * the theory is the one `react-refresh/only-export-components` exists to
 * describe. The rule was switched off for `src/shared/i18n/**` with a
 * comment arguing that splitting a provider from its hook "would make the
 * source worse to read for no runtime benefit". A context object is the one
 * file where that claim is not safe to make.
 *
 * Nothing here is a component, so Fast Refresh will not swap this module —
 * it triggers a full reload instead, which is the safe failure.
 */
export interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  /** Looks up `key`, interpolating `{name}` placeholders from `values`. */
  t: (key: TranslationKey, values?: Record<string, string | number>) => string;
  localeName: (locale: Locale) => string;
}

export const I18nContext = createContext<I18nContextValue | null>(null);

/** Throws outside the provider — see `useTheme` on why not a silent default. */
export function useTranslation(): I18nContextValue {
  const value = use(I18nContext);
  if (value === null) {
    throw new Error("useTranslation must be used inside an I18nProvider.");
  }
  return value;
}
