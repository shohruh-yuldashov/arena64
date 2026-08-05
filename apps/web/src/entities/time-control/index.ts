import type { components } from "@/shared/api/generated/schema";

/**
 * A clock the platform offers — A64-020.5A §4.
 *
 * An alias over the **generated** response, never a re-declaration. The
 * catalogue is `reference.time_control`'s and the four entries it holds
 * today are a migration's, so a hand-written copy here would be a second
 * definition of what "3+2" means — and the first one to drift would win
 * silently on whichever screen read it.
 */
export type TimeControl = components["schemas"]["TimeControlResponse"];

/** The stable code a queue join submits. Never assembled from a duration. */
export type TimeControlId = components["schemas"]["TimeControlId"];

/** Which rating a result under a control moves. The catalogue decides it. */
export type SpeedClass = components["schemas"]["SpeedClass"];

/**
 * `"3+2"` — the control, in the notation every board site uses.
 *
 * ## Why this exists when the API already sends `label`
 *
 * It does, and `label` is the fallback. But a label is a *string the
 * platform typed*, and this is a **formatted number pair**: `Intl` puts the
 * digits in the reader's own numbering system, which matters for the
 * locales this product ships to and which a server-side string cannot do
 * for three languages at once.
 *
 * The arithmetic is the only thing computed here, and it is division by a
 * thousand — not a re-derivation of what the control *means*. Nothing in
 * this file decides a speed class, because SPEC-RATING §19 leaves those
 * boundaries an open product decision and the catalogue carries the answer.
 *
 * Minutes and seconds, because that is how players say it: "three plus
 * two", not "one hundred and eighty thousand milliseconds". A control whose
 * base time is not a whole number of minutes formats with one decimal
 * rather than rounding — no seeded control needs it, and a future 2.5+0
 * would otherwise render as "2+0" and be a lie about the clock.
 */
export function formatTimeControl(control: TimeControl, locale: string): string {
  return clockNotation(control.base_time_ms, control.increment_ms, locale);
}

/**
 * The same, from a pending match's loose columns.
 *
 * `PendingMatchResponse` carries `base_time_ms` and `increment_ms` rather
 * than a `TimeControlResponse`, because a match records a **snapshot** of
 * what was chosen and not a pointer into a catalogue somebody may edit
 * (`reference.domain.time_control`). So the offer dialog formats from two
 * integers, and it must produce the same string the picker did — which is
 * why this delegates rather than repeating the arithmetic.
 *
 * `null` when the match is untimed. That is a real state today: a
 * tournament fixture carries no clock, and the honest answer is to render
 * nothing rather than "0+0".
 */
export function formatMillis(
  baseMs: number | null,
  incrementMs: number | null,
  locale: string,
): string | null {
  if (baseMs === null || incrementMs === null) return null;
  return clockNotation(baseMs, incrementMs, locale);
}

/** The arithmetic both spellings share, so they cannot disagree. */
function clockNotation(baseMs: number, incrementMs: number, locale: string): string {
  const format = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  return `${format.format(baseMs / 60_000)}+${format.format(incrementMs / 1_000)}`;
}
