import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";
import { LoadFailure, Skeleton } from "@/shared/ui";

/**
 * The three states every profile read can be in, in one place.
 *
 * Written once because otherwise each page invents its own — and the ones
 * that get forgotten are the failure state and the retry, so a request that
 * fails renders nothing at all and reads as a blank page.
 *
 * ## Why this is not `ListState` — A64-025.11 §32
 *
 * A profile is not a list, and the difference is the loading state: a
 * heading block with two lines under it, because that is the shape of what
 * arrives. Three identical bars would be a preview of a list that is not
 * coming.
 *
 * There is no empty branch for the same reason — a profile that resolves
 * exists, so "nothing here" is not one of its outcomes.
 *
 * What the two **do** share is the failure, which is `LoadFailure`. That is
 * the branch §3.9 found written six different ways, and the one where the
 * disagreement was not merely cosmetic.
 */
export function QueryState({
  isPending,
  isError,
  errorMessage,
  onRetry,
  children,
}: {
  isPending: boolean;
  isError: boolean;
  /** What failed, in the caller's words. Falls back to the generic sentence. */
  errorMessage?: string;
  onRetry: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();

  if (isPending) {
    return (
      <div className="flex flex-col gap-3">
        <span role="status" className="sr-only">
          {t("state.loading")}
        </span>
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </div>
    );
  }

  if (isError) return <LoadFailure message={errorMessage} onRetry={onRetry} />;

  return <>{children}</>;
}
