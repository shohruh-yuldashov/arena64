import { useTranslation } from "@/shared/i18n";

/**
 * The admin console's page control — A64-024 hardening.
 *
 * **Previous / page number / Next**, and no numbered jumps. Every listing
 * behind it pages by keyset, so there is no cursor for a page nobody has
 * walked to and no total anybody counted. A "57" the operator can click and
 * the server cannot honour would be worse than its absence — see
 * `use-cursor-pages` for the full reasoning.
 *
 * Replaces "Load more", which was correct and unusable: an operator on page
 * nine of an audit trail had eight pages of rows above the one they were
 * reading, and no way back to page three except scrolling.
 *
 * ## Accessibility
 *
 * A `<nav>` with a name, because there is more than one navigation on these
 * pages and a screen reader lists them by name. The page indicator is
 * `aria-live="polite"` so a change is announced without stealing focus —
 * the buttons keep it, which is what lets an operator page through with the
 * keyboard alone.
 *
 * Both buttons are real `<button>`s and are `disabled` at the ends of the
 * walk rather than hidden: a control that disappears moves everything
 * around it, and an operator who was about to click lands on whatever slid
 * into its place.
 */
export function Pagination({
  page,
  hasPrevious,
  hasNext,
  busy,
  onPrevious,
  onNext,
}: {
  page: number;
  hasPrevious: boolean;
  hasNext: boolean;
  busy: boolean;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const { t } = useTranslation();

  // Nothing to navigate: one page, no next. Rendering a disabled pair here
  // would be two controls that can never do anything.
  if (!hasPrevious && !hasNext) return null;

  return (
    <nav className="pagination" aria-label={t("pagination.label")}>
      <button type="button" onClick={onPrevious} disabled={!hasPrevious || busy}>
        {t("pagination.previous")}
      </button>

      <span aria-live="polite">
        {busy ? t("pagination.loading") : t("pagination.page", { page })}
      </span>

      <button type="button" onClick={onNext} disabled={!hasNext || busy}>
        {t("pagination.next")}
      </button>
    </nav>
  );
}
