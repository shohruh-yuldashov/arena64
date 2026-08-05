/**
 * Locale-aware dates and numbers — A64-020.3 §17.
 *
 * `Intl` rather than a formatting library, and rather than hand-built
 * strings. A hand-built `${day}.${month}.${year}` is wrong in at least one
 * of three locales the moment it is written, and `Intl` is in every browser
 * this app supports.
 *
 * This is also the honest half of OQ-2: the missing piece was never date
 * formatting — `Intl` covers it — but **pluralisation**, which the
 * dictionary lookup cannot express. Nothing here needs a plural, so nothing
 * here needs ICU.
 */
export function formatDate(iso: string | null | undefined, locale: string): string | null {
  if (iso === null || iso === undefined) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { dateStyle: "long" }).format(date);
}

/** A date and a time — for "last seen", where the hour is the point. */
export function formatDateTime(iso: string | null | undefined, locale: string): string | null {
  if (iso === null || iso === undefined) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(
    date,
  );
}

export function formatNumber(value: number, locale: string): string {
  return new Intl.NumberFormat(locale).format(value);
}

/**
 * A share as a percentage.
 *
 * The API's `win_rate` is a fraction; `Intl` applies the locale's own
 * percent sign and placement, which is not `${x}%` in every language.
 */
export function formatPercent(fraction: number, locale: string): string {
  return new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(fraction);
}
