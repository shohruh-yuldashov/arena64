import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";
import { Button, Skeleton } from "@/shared/ui";

/**
 * Loading, failure and empty for a social list — written once.
 *
 * The empty state is a **heading and a sentence**, not a decorative icon:
 * an icon alone says nothing to a screen reader and nothing to anybody who
 * has not seen it before.
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
      <div className="flex flex-col gap-2" role="status" aria-label={t("social.state.loading")}>
        <Skeleton className="h-14 w-full" />
        <Skeleton className="h-14 w-full" />
        <Skeleton className="h-14 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <div role="alert" className="flex flex-col items-start gap-3">
        <p className="text-sm font-medium">{t("social.state.failed")}</p>
        <Button variant="outline" className="min-h-11" onClick={onRetry}>
          {t("social.state.retry")}
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
