import type { ReactNode } from "react";

import type { AnalyticsPeriodMeta } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";

/**
 * The analytics presentation primitives — A64-027.6.
 *
 * **Formatting only.** Not one of these computes a rate: every number
 * arrives from a canonical read model that A64-027.3 to .5 tested against
 * real PostgreSQL, and a `completed / started` in here would be a second
 * definition of M10 without M10's abort semantics.
 *
 * ## `null` is a dash, and it is not nought
 *
 * The single most important behaviour on this page. A rate of `null` means
 * the question has no answer — an empty denominator, or a retention window
 * that has not elapsed. Rendering it as `0%` would show a decline that did
 * not happen, which A64-027.4 §33 names as the failure that "is always
 * wrong and always looks like a decline".
 *
 * So `formatRate(null)` is a dash with an accessible explanation, and
 * `formatRate(0)` is `0%`. A test asserts both.
 */

export function formatCount(value: number, locale: string): string {
  return new Intl.NumberFormat(locale).format(value);
}

/** A fraction as a percentage, or `null`. Never `0%` for `null`. */
export function formatRate(value: number | null, locale: string): string | null {
  if (value === null) return null;
  return new Intl.NumberFormat(locale, {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(value);
}

/**
 * Seconds, read the way a person reads a wait.
 *
 * Under a minute keeps one decimal, because the difference between 4.2 s
 * and 4.9 s is the difference this metric exists to show. Above it, whole
 * units: nobody tunes a queue on the seconds of "3m 41s".
 */
export function formatDuration(seconds: number | null, locale: string): string | null {
  if (seconds === null) return null;
  if (seconds < 60)
    return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${String(minutes)}m ${String(Math.round(seconds % 60))}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${String(hours)}h ${String(minutes % 60)}m`;
  return `${String(Math.floor(hours / 24))}d ${String(hours % 24)}h`;
}

/**
 * One figure, with what it means and whether it could be measured.
 *
 * `value === null` renders the dash and an accessible hint rather than a
 * zero — see the module docstring.
 */
export function Metric({
  label,
  value,
  hint,
  detail,
}: {
  label: string;
  value: string | null;
  hint?: string;
  detail?: ReactNode;
}) {
  const { t } = useTranslation();
  const measured = value !== null;

  return (
    <div className="metric">
      <dt>
        {label}
        {hint !== undefined && (
          <span className="metric__hint" title={hint}>
            <span className="sr-only">{hint}</span>
            <span aria-hidden="true">?</span>
          </span>
        )}
      </dt>
      <dd>
        <span className="metric__value" data-unmeasured={measured ? undefined : "true"}>
          {measured ? value : t("analytics.unavailable")}
        </span>
        {!measured && <span className="sr-only">{t("analytics.unavailableHint")}</span>}
        {detail !== undefined && <span className="metric__detail">{detail}</span>}
      </dd>
    </div>
  );
}

/**
 * A horizontal bar, and a number beside it.
 *
 * SVG and CSS rather than a charting dependency — §36. The admin console's
 * whole dependency list is React and a router; a visualisation framework
 * for four bars would cost more than everything it draws.
 *
 * The bar is `aria-hidden` and the figure beside it is real text, so the
 * information is never available only to a sighted mouse user (§53).
 */
export function Bar({
  label,
  value,
  max,
  display,
}: {
  label: string;
  value: number;
  max: number;
  display: string;
}) {
  const share = max > 0 ? Math.max(value / max, 0) : 0;

  return (
    <div className="bar">
      <span className="bar__label">{label}</span>
      <span className="bar__track" aria-hidden="true">
        <span className="bar__fill" style={{ inlineSize: `${String(share * 100)}%` }} />
      </span>
      <span className="bar__value">{display}</span>
    </div>
  );
}

/**
 * A badge for a period's trustworthiness.
 *
 * `partial` and `truncated` are different problems and say so: one means
 * the numbers can still rise, the other that the answer covers a narrower
 * period than the one asked for. Collapsing them into one grey dash is what
 * §23 forbids.
 *
 * Truncation is **not** only pruning. `max(horizon, oldest_retained_day)`
 * also narrows the range when the store simply holds nothing that old — a
 * young environment, which would otherwise be told its data had been
 * deleted. So the badge states the period actually answered rather than a
 * cause it cannot know.
 */
export function PeriodBadge({ meta }: { meta: AnalyticsPeriodMeta }) {
  const { t } = useTranslation();
  const notes: { key: string; label: string; hint: string }[] = [];
  const { maturity, coverage } = meta;

  if (maturity === "partial") {
    notes.push({
      key: "partial",
      label: t("analytics.partial"),
      hint: t("analytics.partialHint"),
    });
  }
  if (coverage === "truncated") {
    notes.push({
      key: "truncated",
      label: t("analytics.truncated"),
      // The effective period, not the requested one: "coverage limited"
      // without saying *to what* leaves an operator comparing a 90-day
      // label against whatever the store happened to hold.
      hint: t("analytics.truncatedHint", {
        start: meta.period_start,
        end: meta.period_end,
      }),
    });
  }
  if (notes.length === 0) return null;

  return (
    <p className="badges">
      {notes.map((note) => (
        <span key={note.key} className="badge" data-kind={note.key}>
          {note.label}
          <span className="sr-only"> — {note.hint}</span>
        </span>
      ))}
    </p>
  );
}
