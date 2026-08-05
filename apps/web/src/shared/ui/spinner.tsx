import { Loader2Icon } from "lucide-react";

import { cn } from "@/shared/lib/cn";

/**
 * A busy indicator that announces itself.
 *
 * `role="status"` with an `aria-label` is what makes this perceivable to a
 * screen reader — a bare spinning icon is silence, and the user is left
 * wondering whether the click registered. The icon itself is
 * `aria-hidden`: it carries no information the label does not.
 */
export function Spinner({
  className,
  label = "Loading",
  ...props
}: React.ComponentProps<"div"> & { label?: string }) {
  return (
    <div
      role="status"
      aria-label={label}
      data-slot="spinner"
      className={cn("inline-flex items-center justify-center", className)}
      {...props}
    >
      <Loader2Icon aria-hidden="true" className="size-4 animate-spin" />
    </div>
  );
}
