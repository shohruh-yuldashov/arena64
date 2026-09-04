import { describe, expect, it } from "vitest";

import { formatDayHeading, formatRelativeTime } from "@/shared/lib/format";

/**
 * The two clock-relative formatters — A64-025.10 §21.
 *
 * Worth a test where `formatNumber` is not: these two contain the only
 * arithmetic in the file, they disagreed with each other on the first
 * attempt, and the disagreement was invisible until a row reading
 * "yesterday" appeared under a heading reading "2 days ago".
 *
 * `now` is injected, so nothing here depends on the wall clock, the day of
 * the week the suite runs on, or how long the suite takes.
 */
/**
 * Built from **local** parts, never from a `Z` string.
 *
 * `formatDayHeading` compares local midnights, which is what a reader means
 * by "today" — and it is exactly why a UTC literal makes the assertion
 * depend on the machine's zone: `2026-09-03T23:00:00Z` is yesterday in
 * London and today in Tashkent. Constructing local dates keeps the suite
 * deterministic in any zone, which CLAUDE.md §6.4 requires.
 */
const local = (year: number, month: number, day: number, hour = 12, minute = 0) =>
  new Date(year, month - 1, day, hour, minute).toISOString();

const NOW = new Date(2026, 8, 4, 10, 20);

describe("formatRelativeTime", () => {
  it("counts days between midnights, not elapsed 24-hour periods", () => {
    // 46 hours: one 24-hour period, two calendar days. Dividing elapsed
    // seconds said "yesterday" while the day heading said "2 days ago" —
    // both true of different quantities, and visibly contradictory on
    // screen. Days are what people mean by days.
    expect(formatRelativeTime(local(2026, 9, 2, 12), "en", NOW)).toBe("2 days ago");
  });

  it("uses hours and minutes inside the same calendar day", () => {
    expect(formatRelativeTime(local(2026, 9, 4, 8, 20), "en", NOW)).toBe("2 hours ago");
    expect(formatRelativeTime(local(2026, 9, 4, 10, 5), "en", NOW)).toBe("15 minutes ago");
  });

  it("says now rather than in 0 seconds", () => {
    expect(formatRelativeTime(local(2026, 9, 4, 10, 20), "en", NOW)).toBe("now");
  });

  it("climbs to weeks, months and years", () => {
    expect(formatRelativeTime(local(2026, 8, 21, 10, 20), "en", NOW)).toBe("2 weeks ago");
    expect(formatRelativeTime(local(2026, 6, 4, 10, 20), "en", NOW)).toBe("3 months ago");
    expect(formatRelativeTime(local(2024, 9, 4, 10, 20), "en", NOW)).toBe("2 years ago");
  });

  it("returns null for an absent or unparseable instant", () => {
    expect(formatRelativeTime(null, "en", NOW)).toBeNull();
    expect(formatRelativeTime(undefined, "en", NOW)).toBeNull();
    expect(formatRelativeTime("not a date", "en", NOW)).toBeNull();
  });
});

describe("formatDayHeading", () => {
  it("names today and yesterday, capitalised", () => {
    // `Intl` returns these lowercase; a heading that starts in lower case
    // reads as an unfinished sentence.
    expect(formatDayHeading(local(2026, 9, 4, 8), "en", NOW)).toBe("Today");
    expect(formatDayHeading(local(2026, 9, 3, 23), "en", NOW)).toBe("Yesterday");
  });

  it("names the weekday inside the last week", () => {
    // "Wednesday" rather than "2 days ago" — the second is arithmetic the
    // reader has to do.
    expect(formatDayHeading(local(2026, 9, 2, 12), "en", NOW)).toBe("Wednesday");
  });

  it("falls back to the date once the words stop helping", () => {
    // Asserted by shape rather than by an exact string: the wording is
    // `Intl`'s and moves between ICU versions, while the branch this test
    // is about is "stopped using words".
    const heading = formatDayHeading(local(2026, 8, 1, 12), "en", NOW) ?? "";
    expect(heading).toMatch(/2026/);
    expect(heading).not.toMatch(/ago|today|yesterday/i);
  });

  it("agrees with the row's own relative time about which day it is", () => {
    // The invariant the first version broke: a row and the heading above it
    // must not disagree about how long ago the same instant was.
    const iso = local(2026, 9, 2, 12);
    expect(formatRelativeTime(iso, "en", NOW)).toBe("2 days ago");
    expect(formatDayHeading(iso, "en", NOW)).toBe("Wednesday");
  });
});
