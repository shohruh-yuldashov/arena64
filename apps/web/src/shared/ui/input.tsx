import type { ComponentProps } from "react";

import { cn } from "@/shared/lib/cn";

/**
 * The shadcn/ui input, unmodified in its accessibility behaviour.
 *
 * `aria-invalid` drives the error styling rather than a `hasError` prop:
 * the attribute is what a screen reader reads, so binding the visual state
 * to it means the two cannot disagree. `focus-visible` — not `focus` —
 * keeps the ring for keyboard users without painting it on every click.
 */
/**
 * A64-025.4 §7: `h-11`, not `h-9`.
 *
 * A64-025.2 put the 44px floor on `Button` and left `Input` at 36 — which
 * is the taller half of the pair a person actually taps on a phone, and
 * measured at 36px on `/login` at 360px. The rule was never "buttons are
 * 44"; it was "player-facing controls are". Fixed height rather than
 * `min-h`, because an input with a min-height and no height renders
 * differently across browsers when it is empty.
 */
export function Input({ className, type, ...props }: ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "border-input file:text-foreground placeholder:text-muted-foreground selection:bg-primary selection:text-primary-foreground dark:bg-input/30 flex h-11 w-full min-w-0 rounded-md border bg-transparent px-3 py-1 text-base shadow-xs transition-[color,box-shadow] outline-none file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
        "focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px]",
        "aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive",
        className,
      )}
      {...props}
    />
  );
}
