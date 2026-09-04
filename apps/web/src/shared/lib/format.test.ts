import { describe, expect, it } from "vitest";

import en from "@/shared/i18n/locales/en.json";
import ru from "@/shared/i18n/locales/ru.json";
import uz from "@/shared/i18n/locales/uz.json";
import {
  formatDate,
  formatDayHeading,
  formatList,
  formatRelativeTime,
} from "@/shared/lib/format";

/**
 * The two clock-relative formatters — A64-025.5D.
 *
 * Worth testing where `formatNumber` is not: these hold the only arithmetic
 * in the file, they disagreed with each other once already, and the reason
 * they no longer use `Intl.RelativeTimeFormat` is a defect that shipped
 * because nobody looked at the product in Uzbek.
 *
 * `now` and every input are built from **local** parts, never from a `Z`
 * literal: the day arithmetic compares local midnights — which is what a
 * reader means by "today" — so a UTC literal would make these assertions
 * depend on the machine's zone. CLAUDE.md §6.4.
 */
const local = (year: number, month: number, day: number, hour = 12, minute = 0, second = 0) =>
  new Date(year, month - 1, day, hour, minute, second).toISOString();

const NOW = new Date(2026, 8, 4, 10, 20);

/** The real dictionaries, so a missing or misspelt key fails here. */
const MESSAGES: Record<string, Record<string, unknown>> = { en, ru, uz };

function translator(locale: string) {
  return (key: string, values?: Record<string, string | number>): string => {
    const text = key
      .split(".")
      .reduce<unknown>(
        (node, part) => (node as Record<string, unknown> | undefined)?.[part],
        MESSAGES[locale],
      );
    if (typeof text !== "string") throw new Error(`missing key ${key} in ${locale}`);
    return text.replace(/\{(\w+)\}/g, (_, name: string) => String(values?.[name] ?? ""));
  };
}

describe("formatRelativeTime", () => {
  it("counts days between midnights, not elapsed 24-hour periods", () => {
    // 46 hours: one 24-hour period, two calendar days. Dividing elapsed
    // seconds said "yesterday" while the heading above the same row said
    // "2 days ago" — both true of different quantities, and contradictory
    // on screen.
    expect(formatRelativeTime(local(2026, 9, 2, 12), "en", translator("en"), NOW)).toBe(
      "2 days ago",
    );
  });

  it("uses hours and minutes inside the same calendar day", () => {
    expect(formatRelativeTime(local(2026, 9, 4, 8, 20), "en", translator("en"), NOW)).toBe(
      "2 hours ago",
    );
    expect(formatRelativeTime(local(2026, 9, 4, 10, 5), "en", translator("en"), NOW)).toBe(
      "15 minutes ago",
    );
  });

  it("says just now under a minute", () => {
    expect(formatRelativeTime(local(2026, 9, 4, 10, 20), "en", translator("en"), NOW)).toBe(
      "just now",
    );
  });

  it("hands back to a date beyond a week", () => {
    // `null` is the signal, not a sentence: "four months ago" is worse than
    // the date it would replace.
    expect(formatRelativeTime(local(2026, 8, 20, 12), "en", translator("en"), NOW)).toBeNull();
  });

  it("speaks Uzbek, which is why this stopped using Intl", () => {
    // Chromium answers `RelativeTimeFormat.supportedLocalesOf(["uz"])` with
    // `["uz"]` and then renders three hours ago as `-3 h` and a day ago as
    // the English word "yesterday". These are the sentences that replaced
    // it, and this test is the reason they exist.
    expect(formatRelativeTime(local(2026, 9, 4, 7, 20), "uz", translator("uz"), NOW)).toBe(
      "3 soat oldin",
    );
    expect(formatRelativeTime(local(2026, 9, 2, 12), "uz", translator("uz"), NOW)).toBe(
      "2 kun oldin",
    );
  });

  it("picks Russian's one, few and many", () => {
    // The one thing genuinely worth taking from the platform:
    // `Intl.PluralRules` has complete data for all three locales, and
    // getting Russian's forms right by hand is a certainty of getting them
    // wrong.
    const t = translator("ru");
    expect(formatRelativeTime(local(2026, 9, 4, 9, 20), "ru", t, NOW)).toBe("1 час назад");
    expect(formatRelativeTime(local(2026, 9, 4, 7, 20), "ru", t, NOW)).toBe("3 часа назад");
    expect(formatRelativeTime(local(2026, 9, 4, 5, 20), "ru", t, NOW)).toBe("5 часов назад");
  });

  it("looks forward as well as back", () => {
    // A64-025.7B. A deadline is the same question asked the other way
    // round, and a tournament list reading "Entries close September 5,
    // 2026" made a reader work out whether that was worth hurrying for.
    expect(formatRelativeTime(local(2026, 9, 6, 10, 20), "en", translator("en"), NOW)).toBe(
      "in 2 days",
    );
    expect(formatRelativeTime(local(2026, 9, 4, 13, 20), "uz", translator("uz"), NOW)).toBe(
      "3 soatdan keyin",
    );
    expect(formatRelativeTime(local(2026, 9, 4, 15, 20), "ru", translator("ru"), NOW)).toBe(
      "через 5 часов",
    );
  });

  it("says soon for a deadline about to pass, not just now", () => {
    // The two directions genuinely need different words under a minute:
    // something that recent just happened; something that close is about to.
    expect(formatRelativeTime(local(2026, 9, 4, 10, 20, 30), "en", translator("en"), NOW)).toBe(
      "very soon",
    );
  });

  it("returns null for an absent or unparseable instant", () => {
    const t = translator("en");
    expect(formatRelativeTime(null, "en", t, NOW)).toBeNull();
    expect(formatRelativeTime(undefined, "en", t, NOW)).toBeNull();
    expect(formatRelativeTime("not a date", "en", t, NOW)).toBeNull();
  });
});

describe("formatList", () => {
  it("joins with the Uzbek conjunction, not the English one", () => {
    // Chromium's `ListFormat` answers `uz` with "and", so the ratings block
    // read "Bullet and Yozishma" — a foreign word inside an Uzbek sentence.
    expect(formatList(["Bullet", "Yozishma"], "uz")).toBe("Bullet va Yozishma");
    expect(formatList(["A", "B", "C"], "uz")).toBe("A, B va C");
    expect(formatList(["A"], "uz")).toBe("A");
    expect(formatList([], "uz")).toBe("");
  });

  it("leaves the locales the browser gets right alone", () => {
    expect(formatList(["A", "B"], "en")).toBe("A and B");
    expect(formatList(["A", "B"], "ru")).toBe("A и B");
  });
});

describe("formatDayHeading", () => {
  it("names today and yesterday, in the reader's language", () => {
    expect(formatDayHeading(local(2026, 9, 4, 8), "en", translator("en"), NOW)).toBe("Today");
    expect(formatDayHeading(local(2026, 9, 3, 15), "uz", translator("uz"), NOW)).toBe("Kecha");
  });

  it("names the weekday inside the last week, capitalised", () => {
    // `Intl.DateTimeFormat` does have Uzbek data — it is only the
    // relative-time patterns that are missing — and it returns the weekday
    // in lower case.
    expect(formatDayHeading(local(2026, 9, 2, 12), "en", translator("en"), NOW)).toBe(
      "Wednesday",
    );
    expect(formatDayHeading(local(2026, 9, 2, 12), "uz", translator("uz"), NOW)).toBe(
      "Chorshanba",
    );
  });

  it("spells Uzbek dates from our own table, not the browser's", () => {
    // The reason this test exists: Chromium resolves `uz` and then answers
    // from CLDR's **root** data — `2026 M09 3` for a date and `Thu` for a
    // long weekday. Node's full ICU has the real values, so every earlier
    // test of this file passed while the product showed `M09` to the
    // language it is built for. Asserting the exact strings is what makes
    // the suite and the browser agree.
    expect(formatDate(local(2026, 9, 3, 12), "uz")).toBe("3-sentabr, 2026");
    expect(formatDayHeading(local(2026, 9, 2, 12), "uz", translator("uz"), NOW)).toBe(
      "Chorshanba",
    );
  });

  it("falls back to the date once the words stop helping", () => {
    // Asserted by shape rather than by an exact string: the wording is
    // `Intl`'s and moves between ICU versions, while the branch this test
    // is about is "stopped using words".
    const heading = formatDayHeading(local(2026, 8, 1, 12), "en", translator("en"), NOW) ?? "";
    expect(heading).toMatch(/2026/);
    expect(heading).not.toMatch(/ago|today|yesterday/i);
  });

  it("agrees with the row's own relative time about which day it is", () => {
    const iso = local(2026, 9, 2, 12);
    expect(formatRelativeTime(iso, "en", translator("en"), NOW)).toBe("2 days ago");
    expect(formatDayHeading(iso, "en", translator("en"), NOW)).toBe("Wednesday");
  });
});
