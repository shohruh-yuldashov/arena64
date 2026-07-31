import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merges conditional class names (`clsx`) and then resolves conflicting
 * Tailwind utilities so the last one wins (`tailwind-merge`) — e.g.
 * `cn("px-2", condition && "px-4")` correctly yields `"px-4"` rather than
 * an invalid `"px-2 px-4"`. The canonical shadcn/ui utility; every
 * `components/ui/*` primitive is generated expecting it at exactly this
 * path (`@/lib/utils`).
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
