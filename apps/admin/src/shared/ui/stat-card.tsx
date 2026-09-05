import type { ReactNode } from "react";

import { Icon, type IconName } from "@/shared/ui/icon";
import type { Tone } from "@/shared/ui/status-badge";

/**
 * One headline figure — A64-027A §7.
 *
 * ## No trend, deliberately
 *
 * There is no `delta`, no arrow and no "vs last month", and the omission is
 * the point: the platform computes no previous period with matching
 * completeness semantics, so any arrow here would be decoration wearing the
 * costume of a measurement. A64-027.6 §15 refused the same thing on the
 * analytics page and this card is built so a future author cannot add one
 * without going through the backend first.
 *
 * ## `foot` is where a figure earns its place
 *
 * A number an operator cannot act on is trivia. The slot below carries the
 * link into whichever console owns the work — "4 awaiting acceptance" is
 * only useful beside a way to go and look at them.
 */
export function StatCard({
  label,
  value,
  icon,
  tone = "primary",
  foot,
  hint,
}: {
  label: string;
  value: ReactNode;
  icon?: IconName;
  tone?: Exclude<Tone, "neutral">;
  foot?: ReactNode;
  hint?: string;
}) {
  return (
    <div className="stat">
      <div className="stat__top">
        {icon !== undefined && (
          <span className="stat__glyph" data-tone={tone}>
            <Icon name={icon} size={16} />
          </span>
        )}
        <span>{label}</span>
        {hint !== undefined && (
          <span className="metric__hint" title={hint}>
            <span className="sr-only">{hint}</span>
            <span aria-hidden="true">?</span>
          </span>
        )}
      </div>
      <div className="stat__value">{value}</div>
      {foot !== undefined && <div className="stat__foot">{foot}</div>}
    </div>
  );
}
