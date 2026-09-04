import type { ReactNode } from "react";

import { cn } from "@/shared/lib/cn";

/**
 * The frame every settings group sits in — A64-025.9C.
 *
 * Each `/settings/*` page was a flat column of labels and controls with
 * nothing grouping them and nothing to align to: at 1280 a 500px select sat
 * in a 1160px column, so two thirds of every row was empty and the eye had
 * no edge to follow down the page. A card with ruled rows gives the column
 * its right-hand edge back and makes "these three belong together" visible
 * rather than implied by spacing alone.
 *
 * It is the same surface the profile's statistics and ratings already use,
 * so settings stop looking like a different product.
 */
export function SettingCard({ children }: { children: ReactNode }) {
  return (
    <div className="border-border bg-card divide-border divide-y overflow-hidden rounded-xl border">
      {children}
    </div>
  );
}

/**
 * One setting: what it is on the left, the control on the right.
 *
 * Stacked below `sm`, where there is no room for two columns and a label
 * squeezed beside a select wraps to three lines.
 *
 * `htmlFor` rather than wrapping the control in a `<label>`: the control is
 * rendered by the caller and may be a `<select>`, a checkbox or a button
 * group, and only the caller knows which of those the label should point
 * at. Passing the id keeps the association explicit and lets a caller that
 * has its own label — a checkbox with the text beside it — pass none.
 */
export function SettingRow({
  label,
  description,
  descriptionId,
  htmlFor,
  control,
  /**
   * Keep the row horizontal at every width.
   *
   * The default stacks below `sm`, which is right for a select — a full
   * width control under its label. A checkbox is small enough to sit beside
   * the text at 360, and stacking it leaves a lone tick floating under a
   * sentence with nothing to attach it to.
   */
  inline = false,
  className,
}: {
  label: string;
  description?: string;
  /**
   * The id to put on the description, when the control needs to point at it
   * with `aria-describedby`.
   *
   * The caller owns the wiring because the caller owns the control: a
   * checkbox whose label says "Show my country" and whose description says
   * *where* it appears needs the second half in its accessible description,
   * not merely near it. Optional, because a row whose description only
   * repeats the label needs no reference at all — and a dangling one is
   * worse than none, since it resolves silently to nothing.
   */
  descriptionId?: string;
  htmlFor?: string;
  control: ReactNode;
  inline?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex gap-3 px-4 py-4 sm:items-center sm:justify-between sm:gap-8 sm:px-6",
        inline ? "flex-row items-center justify-between" : "flex-col sm:flex-row",
        className,
      )}
    >
      <div className="flex min-w-0 flex-col gap-0.5">
        {/* A `<label>` even when `htmlFor` is absent: it is still the
            control's name, and a caller that owns its own labelling passes
            no id rather than a wrong one. */}
        <label htmlFor={htmlFor} className="text-sm font-medium">
          {label}
        </label>
        {description !== undefined && (
          <p id={descriptionId} className="text-muted-foreground max-w-prose text-xs">
            {description}
          </p>
        )}
      </div>
      <div className={cn("shrink-0", inline ? "" : "sm:w-56")}>{control}</div>
    </div>
  );
}

/** A group's heading, above its card. */
export function SettingGroup({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <h2 className="text-base font-semibold">{title}</h2>
        {description !== undefined && (
          <p className="text-muted-foreground text-sm">{description}</p>
        )}
      </div>
      {children}
    </section>
  );
}
