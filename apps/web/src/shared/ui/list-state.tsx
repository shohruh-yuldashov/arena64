import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui/button";
import { Skeleton } from "@/shared/ui/skeleton";

/**
 * Loading, failure and empty for a list — written once, for every list.
 *
 * ## Promoted, not invented — A64-025.2 §11
 *
 * This lived in `features/social` and five social pages used it, while
 * tournaments, history and notifications each wrote the same three branches
 * again. The barrier was not the component: it was that its strings were
 * `social.state.*`, so nothing outside that feature could reuse it without
 * announcing "social" to a player reading a tournament list.
 *
 * The strings are now `state.*` and the component is shared. That is the
 * whole change — the markup, the roles and the three-skeleton loading shape
 * are exactly as they were, because they were right.
 *
 * The empty state is a **heading and a sentence**, not a decorative icon:
 * an icon alone says nothing to a screen reader and nothing to anybody who
 * has not seen it before.
 *
 * `emptyTitle` and `emptyHint` stay the caller's, and deliberately: "no
 * friends yet" and "no tournaments open" are domain sentences, and a
 * generic primitive that owned them would be this module holding vocabulary
 * that belongs to five features.
 */
export function ListState({
  isPending,
  isError,
  isEmpty,
  emptyTitle,
  emptyHint,
  onRetry,
  children,
}: {
  isPending: boolean;
  isError: boolean;
  isEmpty: boolean;
  emptyTitle: string;
  emptyHint?: string;
  onRetry: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();

  if (isPending) {
    return (
      <div className="flex flex-col gap-2" role="status" aria-label={t("state.loading")}>
        <Skeleton className="h-14 w-full" />
        <Skeleton className="h-14 w-full" />
        <Skeleton className="h-14 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <div role="alert" className="flex flex-col items-start gap-3">
        <p className="text-sm font-medium">{t("state.failed")}</p>
        <Button variant="outline" onClick={onRetry}>
          {t("state.retry")}
        </Button>
      </div>
    );
  }

  if (isEmpty) {
    return (
      <div className="flex flex-col gap-1 py-8">
        <h2 className="text-base font-medium">{emptyTitle}</h2>
        {emptyHint !== undefined && (
          <p className="text-muted-foreground text-sm">{emptyHint}</p>
        )}
      </div>
    );
  }

  return <>{children}</>;
}
