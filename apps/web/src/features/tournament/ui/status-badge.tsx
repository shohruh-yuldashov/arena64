import { statusKey } from "@/features/tournament/ui/labels";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";

/**
 * A tournament's status, as one badge — A64-025.7.
 *
 * Extracted because there were two of these and they did not agree: the
 * lobby card drew a bordered pill and the detail page put the same fact into
 * a run-on subtitle, so the surface a player lands on after clicking a card
 * dropped the treatment the card had just taught them.
 *
 * **The word carries the meaning; the colour only reinforces it.** §24 asks
 * that a bracket be understandable without colour and the same rule applies
 * here — every state renders its own label, and only the two an operator or
 * a player can act on take a tint.
 */
export function TournamentStatusBadge({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  const { t } = useTranslation();

  return (
    <span
      className={cn(
        "rounded-full border px-2 py-0.5 text-xs font-medium",
        status === "registration_open" && "border-primary text-primary",
        status === "in_progress" && "border-success text-success",
        status === "completed" && "text-muted-foreground",
        status === "cancelled" && "text-muted-foreground line-through",
        className,
      )}
    >
      {t(statusKey(status))}
    </span>
  );
}
