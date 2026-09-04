import { useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui/button";
import { Notice } from "@/shared/ui/notice";

/**
 * A read that failed, and the way to try it again — A64-025.11 §32.
 *
 * ## Why this is its own component
 *
 * §3.9 counted the same three branches written seventy-four times, and the
 * failure branch is the one that diverged most: six surfaces rendered it six
 * ways — `Notice tone="error"` on `/games/history`, a bare block with
 * `text-sm` on `/tournaments`, the same block with `font-medium` in
 * `ListState`, a `<p role="alert">` inside a plain `<div>` in the
 * notification list, and **nothing at all** in the tournament history, where
 * a failed request rendered as an empty list.
 *
 * That last one is why this exists as a component rather than as a
 * convention. A convention can be forgotten silently and the symptom is a
 * broken list that looks healthy; a component that a caller must pass
 * `onRetry` to cannot be half-used. §3.9 notes `apps/admin` has had an
 * `ErrorNotice` since the beginning and `apps/web`, the larger app, has not.
 *
 * ## What it never shows
 *
 * The error's own text. A player gets a sentence they can act on; the
 * diagnostic detail goes to `reportError` (CLAUDE.md §9.7). A status code or
 * a stack in the interface tells the one person who cannot use it.
 *
 * ## The message is the caller's
 *
 * "Tournaments could not be loaded" tells a player which of the three lists
 * on their screen is missing; "We could not load this" does not. Those
 * strings already existed per surface and are better than the generic one,
 * so the generic one is the **fallback** rather than the rule.
 */
export function LoadFailure({
  message,
  onRetry,
  retryLabel,
}: {
  /** What failed, in the caller's words. Defaults to the generic sentence. */
  message?: string;
  onRetry: () => void;
  /** Overridden only where "try again" is not what the button does. */
  retryLabel?: string;
}) {
  const { t } = useTranslation();

  return (
    // The role comes from the tone — `Notice` makes `error` assertive — so
    // it is not stated here. A failed read is something the player asked
    // for and did not get, which is the case an interruption is for.
    <Notice tone="error" className="flex flex-col items-start gap-3">
      <p>{message ?? t("state.failed")}</p>
      <Button variant="outline" className="min-h-11" onClick={onRetry}>
        {retryLabel ?? t("state.retry")}
      </Button>
    </Notice>
  );
}
