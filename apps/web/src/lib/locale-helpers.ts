import type { AppLocale } from "@/i18n/routing";

/**
 * Localization helpers that aren't translation lookups — `next-intl`'s
 * `useTranslations` already covers message strings; what's missing is
 * everything else a locale changes: a human-readable name for the locale
 * itself, and locale-aware formatting for dates and numbers a match
 * timestamp or a rating will need the moment either exists.
 */

/** Each locale's own name for itself — not translated per-viewer, the way
 * a language picker in every major application labels its options. */
export const LOCALE_LABELS: Record<AppLocale, string> = {
  en: "English",
  ru: "Русский",
  uz: "O'zbekcha",
};

export function getLocaleDisplayName(locale: AppLocale): string {
  return LOCALE_LABELS[locale];
}

/**
 * `Intl.DateTimeFormat` bound to a specific locale — a thin wrapper
 * because the three call sites of "format a date" in this app (server
 * components, client components, and eventually a `useFormatter`-based
 * hook) all resolve `locale` differently. `next-intl`'s own `useFormatter`
 * covers the common case inside a component; this covers formatting a
 * date outside one — a log line, a non-component utility.
 */
export function formatDate(
  date: Date | number,
  locale: AppLocale,
  options?: Intl.DateTimeFormatOptions,
): string {
  return new Intl.DateTimeFormat(locale, options).format(date);
}

export function formatNumber(
  value: number,
  locale: AppLocale,
  options?: Intl.NumberFormatOptions,
): string {
  return new Intl.NumberFormat(locale, options).format(value);
}
