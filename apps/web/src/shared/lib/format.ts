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
 * "Bullet, Blitz and Rapid" — a list, joined the way the locale joins one.
 *
 * `join(", ")` is a Latin-script assumption: the separator, the spacing and
 * whether the last item takes a conjunction all differ by language, and
 * none of it belongs in a translation string a translator has to remember
 * to punctuate. `Intl` already knows.
 */
export function formatList(items: string[], locale: string): string {
  return new Intl.ListFormat(locale, { style: "long", type: "conjunction" }).format(items);
}

/**
 * "2 hours ago" — how long ago, in the reader's language.
 *
 * A notification feed is read for recency, and an absolute timestamp makes
 * the reader do the subtraction: "Sep 4, 2026, 1:40 PM" is the same number
 * of words as "2 hours ago" and answers a different question. The absolute
 * time stays on the `<time>` element's `dateTime` and `title`, so nothing is
 * lost — it is demoted, not removed.
 *
 * `Intl.RelativeTimeFormat` supplies every string, including "yesterday" and
 * "last week", so this adds no translations to maintain and is correct in
 * locales whose plural rules are not English's.
 *
 * ## Days are calendar days, not 86,400 seconds
 *
 * The first version divided elapsed seconds, which put a row reading
 * "yesterday" under a heading reading "2 days ago": 46 hours is one
 * 24-hour period and two calendar days, and both statements were true of
 * different quantities. People mean calendar days, and the heading and the
 * row have to mean the same thing — so anything a day or more apart is
 * measured in days between midnights, and only hours and below come from
 * elapsed time.
 *
 * `now` is injected so a test does not depend on the wall clock.
 */
function calendarDaysBetween(from: Date, to: Date): number {
  const midnight = (value: Date) =>
    new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  return Math.round((midnight(from) - midnight(to)) / 86_400_000);
}

export function formatRelativeTime(
  iso: string | null | undefined,
  locale: string,
  now: Date = new Date(),
): string | null {
  if (iso === null || iso === undefined) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;

  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const days = calendarDaysBetween(date, now);

  if (days !== 0) {
    if (Math.abs(days) >= 365) return formatter.format(Math.trunc(days / 365), "year");
    if (Math.abs(days) >= 30) return formatter.format(Math.trunc(days / 30), "month");
    if (Math.abs(days) >= 7) return formatter.format(Math.trunc(days / 7), "week");
    return formatter.format(days, "day");
  }

  // Same calendar day: hours, then minutes, then "now".
  const seconds = Math.round((date.getTime() - now.getTime()) / 1000);
  if (Math.abs(seconds) >= 3600) return formatter.format(Math.trunc(seconds / 3600), "hour");
  if (Math.abs(seconds) >= 60) return formatter.format(Math.trunc(seconds / 60), "minute");
  return formatter.format(0, "second");
}

/**
 * "Today", "Yesterday", "Wednesday", or the date — the heading a day of
 * rows sits under.
 *
 * Three registers, because each is the clearest at its own distance. Today
 * and yesterday have names everyone uses. Inside the last week a weekday
 * beats "4 days ago", which is arithmetic the reader has to do. Beyond it
 * the words stop helping and the date is what somebody would look for.
 *
 * Capitalised for the reader's own locale rather than with `toUpperCase`:
 * `Intl` returns these lowercase, and a heading that begins in lower case
 * reads as an unfinished sentence.
 */
export function formatDayHeading(
  iso: string,
  locale: string,
  now: Date = new Date(),
): string | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;

  const days = calendarDaysBetween(date, now);
  if (days > 0 || days <= -7) return formatDate(iso, locale);

  const text =
    days >= -1
      ? new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(days, "day")
      : new Intl.DateTimeFormat(locale, { weekday: "long" }).format(date);

  return text.charAt(0).toLocaleUpperCase(locale) + text.slice(1);
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
