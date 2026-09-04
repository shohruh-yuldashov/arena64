import type { TranslationKey } from "@/shared/i18n";

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
/**
 * Uzbek calendar names, because the browser does not have them.
 *
 * Chromium resolves `uz` and then answers from CLDR's **root** data: a
 * date formats as `2026 M09 3` and a long weekday as `Thu`. Node's full
 * ICU has the real values — `3-sentabr, 2026` and `chorshanba` — which is
 * why every test of this file passed while the product was showing `M09`
 * to the language it is built for. A suite that runs in Node cannot see a
 * gap in the browser's data, and this table is what makes the two agree.
 *
 * Calendar data rather than product copy, so it lives beside the formatter
 * instead of in `locales/`: nothing here is a sentence anybody wrote, and
 * a translator asked to review "sentabr" would be being asked to check the
 * Gregorian calendar. The forms are exactly what full ICU produces, so if
 * Chromium ever ships the data this can be deleted without a visible
 * change.
 */
const UZ_MONTHS = [
  "yanvar",
  "fevral",
  "mart",
  "aprel",
  "may",
  "iyun",
  "iyul",
  "avgust",
  "sentabr",
  "oktabr",
  "noyabr",
  "dekabr",
];

/** Sunday first, to index by `Date.getDay()` without arithmetic. */
const UZ_WEEKDAYS = [
  "yakshanba",
  "dushanba",
  "seshanba",
  "chorshanba",
  "payshanba",
  "juma",
  "shanba",
];

function isUzbek(locale: string): boolean {
  return locale === "uz" || locale.startsWith("uz-");
}

function uzbekDate(date: Date): string {
  return `${date.getDate().toString()}-${UZ_MONTHS[date.getMonth()]}, ${date.getFullYear().toString()}`;
}

export function formatDate(iso: string | null | undefined, locale: string): string | null {
  if (iso === null || iso === undefined) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  if (isUzbek(locale)) return uzbekDate(date);
  return new Intl.DateTimeFormat(locale, { dateStyle: "long" }).format(date);
}

/** A date and a time — for "last seen", where the hour is the point. */
export function formatDateTime(iso: string | null | undefined, locale: string): string | null {
  if (iso === null || iso === undefined) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  // The clock is fine in Uzbek — `15:30` — it is only the calendar names
  // that are missing, so only those are replaced.
  if (isUzbek(locale)) {
    const time = new Intl.DateTimeFormat(locale, { timeStyle: "short" }).format(date);
    return `${uzbekDate(date)}, ${time}`;
  }
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
  // Uzbek again — A64-025.7B §25.2. Chromium's `ListFormat` answers `uz`
  // with the **English** conjunction, so "Bullet and Yozishma" reached the
  // ratings block: a foreign word in the middle of an Uzbek sentence, which
  // is worse than a missing one because it looks deliberate.
  if (isUzbek(locale)) {
    if (items.length === 0) return "";
    if (items.length === 1) return items[0] ?? "";
    return `${items.slice(0, -1).join(", ")} va ${items.at(-1) ?? ""}`;
  }
  return new Intl.ListFormat(locale, { style: "long", type: "conjunction" }).format(items);
}

/**
 * How long ago, from **our own strings** — A64-025.5D.
 *
 * ## Why not `Intl.RelativeTimeFormat`
 *
 * §21 used it and said it "adds no translations to maintain". That was
 * true of English and Russian and **false of Uzbek**, which is this
 * product's first language. Chromium answers
 * `RelativeTimeFormat.supportedLocalesOf(["uz"])` with `["uz"]` and then
 * has no patterns for it: three hours ago rendered as `-3 h`, and a day ago
 * rendered as the English word "yesterday". Neither failed, which is why it
 * shipped — it took a screenshot in Uzbek to see it.
 *
 * So the sentences are ours and `Intl.PluralRules` picks the form, which is
 * the part genuinely worth taking from the platform: Russian needs
 * one/few/many and getting that wrong by hand is a certainty. Chromium's
 * plural data for all three locales is complete, and was checked before
 * this was written rather than assumed.
 *
 * ## Only up to a week
 *
 * Minutes, hours and days, then `null` — the caller falls back to a date.
 * "Four months ago" is worse than the date it replaces, and stopping at a
 * week is also what keeps this to three units rather than six.
 *
 * ## Days are calendar days
 *
 * Not elapsed seconds divided by 86,400. 46 hours is one 24-hour period and
 * two calendar days, and the first version of this said "yesterday" in a
 * row sitting under a heading that said "2 days ago". People mean calendar
 * days, and a row and its heading have to mean the same thing.
 *
 * `now` is injected so a test does not depend on the wall clock.
 */
/**
 * The app's own `t`, so a key that does not exist is a type error here and
 * not a blank on screen. The two lookups below build their keys from a
 * plural form and are cast at the point of use, which is the same trade
 * every other dynamic lookup in this codebase makes — and the tests read
 * the real dictionaries, so a misspelt one fails there.
 */
type Translate = (key: TranslationKey, values?: Record<string, string | number>) => string;

function calendarDaysBetween(from: Date, to: Date): number {
  const midnight = (value: Date) =>
    new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  return Math.round((midnight(from) - midnight(to)) / 86_400_000);
}

function relativeKey(
  locale: string,
  direction: "ago" | "in",
  unit: "minute" | "hour" | "day",
  count: number,
): TranslationKey {
  const form = new Intl.PluralRules(locale).select(count);
  return `time.${direction}.${unit}.${form}` as TranslationKey;
}

export function formatRelativeTime(
  iso: string | null | undefined,
  locale: string,
  t: Translate,
  now: Date = new Date(),
): string | null {
  if (iso === null || iso === undefined) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;

  // Both directions, from one place — A64-025.7B. A deadline is the same
  // question as a timestamp asked the other way round, and a tournament
  // list that said "Entries close September 5, 2026" was making a reader
  // work out whether that is worth hurrying for.
  const signedDays = calendarDaysBetween(date, now);
  const future = date.getTime() > now.getTime();
  const direction = future ? "in" : "ago";

  const days = Math.abs(signedDays);
  if (days >= 7) return null;
  if (days >= 1) return t(relativeKey(locale, direction, "day", days), { count: days });

  const minutes = Math.floor(Math.abs(date.getTime() - now.getTime()) / 60_000);
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    return t(relativeKey(locale, direction, "hour", hours), { count: hours });
  }
  if (minutes >= 1) {
    return t(relativeKey(locale, direction, "minute", minutes), { count: minutes });
  }
  // Under a minute has two different meanings and needs two words: a
  // timestamp that recent just happened; a deadline that close is about to
  // pass, and "just now" would be exactly wrong.
  return t(future ? "time.soon" : "time.now");
}

/**
 * "Today", "Yesterday", "Wednesday", or the date — the heading a day of
 * rows sits under.
 *
 * Three registers, because each is clearest at its own distance. Today and
 * yesterday have names everyone uses. Inside the last week a weekday beats
 * "4 days ago", which is arithmetic the reader has to do. Beyond it the
 * words stop helping and the date is what somebody would look for.
 *
 * The weekday comes from `Intl.DateTimeFormat`, which **does** have Uzbek
 * data — it is only the relative-time patterns that are missing — and it is
 * capitalised for the reader's own locale, because `Intl` returns it
 * lowercase and a heading that begins in lower case reads as an unfinished
 * sentence.
 */
export function formatDayHeading(
  iso: string,
  locale: string,
  t: Translate,
  now: Date = new Date(),
): string | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;

  const days = calendarDaysBetween(date, now);
  if (days > 0 || days <= -7) return formatDate(iso, locale);
  if (days === 0) return t("time.today");
  if (days === -1) return t("time.yesterday");

  const weekday = isUzbek(locale)
    ? UZ_WEEKDAYS[date.getDay()]
    : new Intl.DateTimeFormat(locale, { weekday: "long" }).format(date);
  return (weekday ?? "").charAt(0).toLocaleUpperCase(locale) + (weekday ?? "").slice(1);
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
