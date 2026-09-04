import type { ReactNode } from "react";

import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { LoadFailure } from "@/shared/ui/load-failure";
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
 * ## Made to fit the surfaces that were writing their own — A64-025.11 §32
 *
 * Promoting it was not enough, because three things about it were decided
 * for social lists and were wrong for the rest:
 *
 * | | Before | Now |
 * | --- | --- | --- |
 * | Skeleton | three rows, `h-14`, always | `pendingRows` × `pendingRowClassName` |
 * | Announcement | `aria-label` on the wrapper | the caller's sentence, as real text |
 * | Failure | its own block | `LoadFailure`, shared with `QueryState` |
 *
 * The skeleton is the reason `/tournaments` kept its own: a tournament card
 * is 96px tall and three 56px bars in its place is not a preview of the
 * list, it is a different list. A loading state that does not match the
 * shape it precedes makes the page jump when the data lands.
 *
 * The announcement is real text in an `sr-only` element rather than an
 * `aria-label` on an empty `<div>`, because a live region with no text
 * content is announced inconsistently, and "Loading tournaments…" is worth
 * more to somebody who cannot see the skeletons than "Loading…".
 *
 * ## The empty state is a placeholder, not a paragraph
 *
 * A heading and a sentence on a dashed panel, filling the space the list
 * would have filled. It was a bare left-aligned block, which collapses the
 * page to nothing and reads as a sentence that lost its container — the
 * notification list and the tournament history had both already moved to a
 * dashed panel independently, which is the product telling us which one was
 * right.
 *
 * No decorative icon, here or in `Notice`: an icon alone says nothing to a
 * screen reader and nothing to anybody who has not seen it before.
 *
 * `emptyTitle` and `emptyHint` stay the caller's, and deliberately: "no
 * friends yet" and "no tournaments open" are domain sentences, and a
 * generic primitive that owned them would be this module holding vocabulary
 * that belongs to five features.
 *
 * ## `emptyAction` — A64-025.8
 *
 * Optional, and only some empty states should pass one. "Find players and
 * send them a request" names something to do and then left the player to
 * find the way themselves; "when a friend invites you to a game, it appears
 * here" names nothing, because there is nothing to do but wait.
 *
 * A button under the second would be an invented next step. The slot exists
 * so the first can offer what its own sentence promised — not so every
 * empty list grows a button.
 */
export function ListState({
  isPending,
  isError,
  isEmpty,
  loadingLabel,
  errorMessage,
  emptyTitle,
  emptyHint,
  emptyAction,
  pendingRows = 3,
  pendingRowClassName = "h-16",
  onRetry,
  children,
}: {
  isPending: boolean;
  isError: boolean;
  isEmpty: boolean;
  /** "Loading tournaments…" — announced, never shown. Falls back to "Loading…". */
  loadingLabel?: string;
  /** "Tournaments could not be loaded." Falls back to the generic sentence. */
  errorMessage?: string;
  emptyTitle: string;
  emptyHint?: string;
  /** Pass one only where `emptyHint` names something the player can do. */
  emptyAction?: ReactNode;
  /** As many bars as the list usually shows above the fold. */
  pendingRows?: number;
  /** The height of one row, so the skeleton is a preview and not a guess. */
  pendingRowClassName?: string;
  onRetry: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();

  if (isPending) {
    return (
      <div className="flex flex-col gap-2">
        <span role="status" className="sr-only">
          {loadingLabel ?? t("state.loading")}
        </span>
        {Array.from({ length: pendingRows }, (_, row) => (
          <Skeleton key={row} className={cn("w-full", pendingRowClassName)} />
        ))}
      </div>
    );
  }

  if (isError) return <LoadFailure message={errorMessage} onRetry={onRetry} />;

  if (isEmpty) {
    return (
      <div className="border-border flex flex-col items-center gap-1 rounded-xl border border-dashed px-5 py-12 text-center">
        <h2 className="text-base font-medium">{emptyTitle}</h2>
        {emptyHint !== undefined && (
          <p className="text-muted-foreground text-sm">{emptyHint}</p>
        )}
        {emptyAction !== undefined && <div className="pt-3">{emptyAction}</div>}
      </div>
    );
  }

  return <>{children}</>;
}
