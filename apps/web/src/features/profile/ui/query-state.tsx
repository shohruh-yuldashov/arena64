import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";
import { Button, Skeleton } from "@/shared/ui";

/**
 * The three states every profile read can be in, in one place.
 *
 * Written once because otherwise each page invents its own — and the ones
 * that get forgotten are the failure state and the retry, so a request that
 * fails renders nothing at all and reads as a blank page.
 *
 * A **bounded** failure: a message and a way to try again, never the
 * error's own text. That belongs in `reportError`, not on screen
 * (CLAUDE.md §9.7).
 */
export function QueryState({
  isPending,
  isError,
  onRetry,
  children,
}: {
  isPending: boolean;
  isError: boolean;
  onRetry: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();

  if (isPending) {
    return (
      <div
        className="flex flex-col gap-3"
        role="status"
        aria-label={t("profile.state.loading")}
      >
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </div>
    );
  }

  if (isError) {
    return (
      <div role="alert" className="flex flex-col items-start gap-3">
        <p className="text-sm font-medium">{t("profile.state.failedTitle")}</p>
        <Button variant="outline" className="min-h-11" onClick={onRetry}>
          {t("profile.state.retry")}
        </Button>
      </div>
    );
  }

  return <>{children}</>;
}
