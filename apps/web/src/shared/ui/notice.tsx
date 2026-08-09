import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/shared/lib/cn";

/**
 * One short message about something that just happened — A64-025.2 §10.
 *
 * ## Why this exists
 *
 * `specs/product-experience.md` §3.9: `role="alert"` is written out 34
 * times across 24 files and `role="status"` 40 times across 23, each with
 * its own spacing and its own idea of what an error looks like. None of
 * them is wrong; they simply do not agree, and nothing makes them agree.
 *
 * ## The tone chooses the role, and that is the point
 *
 * An `error` is `role="alert"` — assertive, interrupting, because something
 * the player did has failed. Everything else is `role="status"` — polite,
 * announced when the reader pauses. Getting this backwards is the commonest
 * accessibility defect in a feedback component: a success message that
 * interrupts a screen reader mid-sentence is worse than no message.
 *
 * The caller may still override `role` for the case this rule does not fit.
 *
 * ## Never colour alone
 *
 * Every tone carries a border and a background *and* the words the caller
 * passes. A player who cannot distinguish the four tints reads the same
 * sentence either way — which is the rule `specs/product-experience.md` §5
 * states and the board already honours. There is no icon here for the same
 * reason `ListState`'s empty state has none: an icon alone says nothing to
 * a screen reader, and an icon beside a sentence that already says it is
 * decoration this component does not need to own.
 */
const noticeVariants = cva("rounded-md border px-3 py-2 text-sm", {
  variants: {
    tone: {
      info: "bg-muted/50 border-border text-foreground",
      success: "border-success/40 bg-success/10 text-foreground",
      warning: "border-warning/40 bg-warning/10 text-foreground",
      error: "border-destructive/40 bg-destructive/10 text-foreground",
    },
  },
  defaultVariants: { tone: "info" },
});

export function Notice({
  tone = "info",
  title,
  className,
  children,
  role,
  ...props
}: ComponentProps<"div"> & VariantProps<typeof noticeVariants> & { title?: ReactNode }) {
  return (
    <div
      // Assertive only for a failure; polite for everything else.
      role={role ?? (tone === "error" ? "alert" : "status")}
      className={cn(noticeVariants({ tone }), className)}
      {...props}
    >
      {title !== undefined && <p className="font-medium">{title}</p>}
      {children}
    </div>
  );
}
