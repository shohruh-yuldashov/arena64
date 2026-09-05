import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";
import { Icon, type IconName } from "@/shared/ui/icon";

/**
 * The four states every console page owes its reader — A64-027A §28.
 *
 * Before this task each page invented its own, and two of them invented
 * nothing: a section with no rows rendered as an empty region, which is
 * indistinguishable from a section that failed to load. That ambiguity is
 * the whole reason these are primitives rather than markup repeated eight
 * times — an operator must never have to guess whether "nothing here" means
 * "nothing happened" or "we could not ask".
 */

/** Nothing to show, and that is a legitimate answer. */
export function EmptyState({
  icon = "info",
  title,
  description,
  action,
}: {
  icon?: IconName;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="state">
      <span className="state__glyph">
        <Icon name={icon} size={20} />
      </span>
      <h3>{title}</h3>
      {description !== undefined && <p>{description}</p>}
      {action}
    </div>
  );
}

/**
 * Something failed, and the reader is told what to do next.
 *
 * `onRetry` is optional because not every failure is worth retrying — a
 * refused request will be refused again, and a button that re-fails is
 * worse than no button.
 */
export function ErrorState({
  title,
  description,
  onRetry,
}: {
  title: string;
  description?: string;
  onRetry?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="state" role="alert">
      <span className="state__glyph" data-tone="danger">
        <Icon name="warning" size={20} />
      </span>
      <h3>{title}</h3>
      {description !== undefined && <p>{description}</p>}
      {onRetry !== undefined && (
        <button type="button" className="action" onClick={onRetry}>
          <Icon name="refresh" size={16} />
          {t("state.retry")}
        </button>
      )}
    </div>
  );
}

/**
 * A skeleton in the shape of what is coming.
 *
 * Deliberately shaped rather than a spinner: a block the size of the table
 * that is loading tells the reader the page is not about to reflow under
 * them. `role="status"` with an accessible label, because a screen reader
 * gets nothing at all from a grey rectangle.
 */
export function LoadingSkeleton({ rows = 5, label }: { rows?: number; label?: string }) {
  const { t } = useTranslation();
  return (
    <div role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label ?? t("state.loading")}</span>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
        {Array.from({ length: rows }, (_, index) => (
          <div
            key={index}
            className="skeleton"
            style={{
              blockSize: index === 0 ? "2.25rem" : "3rem",
              // Slight variation, so a loading table does not read as a
              // striped graphic that someone might mistake for content.
              inlineSize: index === 0 ? "40%" : "100%",
            }}
          />
        ))}
      </div>
    </div>
  );
}

/** A banner, for a fact that qualifies the page rather than replacing it. */
export function InfoBanner({
  tone = "info",
  icon,
  children,
}: {
  tone?: "info" | "warning" | "success";
  icon?: IconName;
  children: ReactNode;
}) {
  const glyph: IconName =
    icon ?? (tone === "warning" ? "warning" : tone === "success" ? "success" : "info");
  return (
    <p className="notice" data-tone={tone}>
      <Icon name={glyph} size={17} />
      <span>{children}</span>
    </p>
  );
}
