import { Fragment } from "react";

import type { Notification } from "@/features/notifications/api";
import {
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotifications,
  useUnreadCount,
} from "@/features/notifications/model/queries";
import { NotificationRow } from "@/features/notifications/ui/notification-row";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { formatDayHeading } from "@/shared/lib/format";
import { Button, Skeleton, Spinner } from "@/shared/ui";

/**
 * The notification list — A64-021.1 §20, §22, §28, §29.
 *
 * One column at every width (§29): a notification is a sentence and a
 * timestamp, and a second column would only ever hold whitespace on a
 * phone. The page bounds the width; this bounds nothing.
 *
 * ## Five states, and each one says something different
 *
 *     loading      three skeleton rows, announced as busy
 *     error        a message and a retry — never a silent empty list
 *     empty        "you are all caught up", which is a result and not a fault
 *     page         the rows, plus "load more" while there is more
 *     end          a quiet line saying there is no more
 *
 * The empty state is deliberately not the same as the error state: a player
 * with no notifications and a player whose request failed are in completely
 * different situations, and rendering both as "nothing here" is how a
 * broken list looks healthy.
 *
 * ## Marking read is not the same action as opening
 *
 * Opening a notification marks it read *and* navigates, and the mark is
 * fired without being awaited (§21). "Mark all as read" is a separate
 * control that navigates nowhere, disables itself while it runs, and
 * reconciles the badge afterwards.
 */
export function NotificationList() {
  const { t, locale } = useTranslation();
  const notifications = useNotifications();
  const unread = useUnreadCount();
  const { markRead, error: markError } = useMarkNotificationRead();
  const markAll = useMarkAllNotificationsRead();

  const entries: Notification[] = (notifications.data?.pages ?? []).flatMap(
    (page) => page.entries,
  );
  const hasUnread = (unread.data?.unread_count ?? 0) > 0;

  if (notifications.isPending) {
    return (
      <div
        role="status"
        aria-label={t("notifications.loading")}
        className="flex flex-col gap-2"
      >
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-16 w-full" />
        ))}
      </div>
    );
  }

  if (notifications.isError) {
    return (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-sm">
          {t("notifications.listError")}
        </p>
        <Button
          variant="outline"
          className="min-h-11"
          onClick={() => void notifications.refetch()}
        >
          {t("common.retry")}
        </Button>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="border-border rounded-xl border border-dashed px-5 py-12 text-center">
        <p className="text-sm font-medium">{t("notifications.emptyTitle")}</p>
        <p className="text-muted-foreground mt-1 text-sm">
          {t("notifications.emptyDescription")}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Rendered only when something is unread: a disabled "mark all as
          read" on an already-read list is a control that can never do
          anything, which is worse than an absent one. */}
      {hasUnread && (
        // The count on the left gives the button a partner — A64-025.10
        // §21. A control alone against the right margin has nothing to
        // align to and reads as an afterthought; beside the number it is
        // answering, it reads as the response to it.
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-muted-foreground text-sm">
            {t("notifications.unreadCount", { count: unread.data?.unread_count ?? 0 })}
          </p>
          <Button
            variant="ghost"
            size="sm"
            className="min-h-11"
            disabled={markAll.isPending}
            onClick={() => markAll.mutate()}
          >
            {markAll.isPending ? (
              <Spinner label={t("notifications.markingAll")} />
            ) : (
              t("notifications.markAllRead")
            )}
          </Button>
        </div>
      )}

      {/* A mutation failure is announced where it happened and does not
          replace the list — the notifications a player can already see stay
          on screen (§22). */}
      {(markError !== null || markAll.isError) && (
        <p role="alert" className="text-destructive text-sm">
          {t("notifications.markReadError")}
        </p>
      )}

      {/* Grouped by day, in one card — A64-025.10 §21.

          **One list, not one per day.** The day heading is a visual aid:
          every row already carries its own `<time>`, so a screen reader
          loses nothing by not hearing the separator, and it keeps a single
          list whose length is the number of notifications rather than
          however many days they happen to span. `role="presentation"` is
          what takes the separator out of the list without taking it off the
          screen.

          The card is the same surface every other list in the product now
          uses; these rows were floating on the page background with
          hairline rules and nothing containing them. */}
      <div className="border-border bg-card overflow-hidden rounded-xl border">
        <ul
          aria-label={t("notifications.title")}
          className="divide-border flex flex-col divide-y"
        >
          {groupByDay(entries, locale, t).map((group) => (
            <Fragment key={group.heading}>
              <li role="presentation" aria-hidden="true">
                <p className="text-muted-foreground bg-muted/40 px-4 py-2 text-xs font-medium sm:px-5">
                  {group.heading}
                </p>
              </li>
              {group.entries.map((notification) => (
                <NotificationRow
                  key={notification.id}
                  notification={notification}
                  onOpen={(opened) => markRead(opened.id, opened.is_read)}
                />
              ))}
            </Fragment>
          ))}
        </ul>
      </div>

      {notifications.hasNextPage ? (
        <Button
          variant="outline"
          className="min-h-11 self-center"
          disabled={notifications.isFetchingNextPage}
          onClick={() => void notifications.fetchNextPage()}
        >
          {notifications.isFetchingNextPage ? (
            <Spinner label={t("notifications.loading")} />
          ) : (
            t("notifications.loadMore")
          )}
        </Button>
      ) : (
        <p className="text-muted-foreground text-center text-xs">
          {t("notifications.endOfList")}
        </p>
      )}
    </div>
  );
}

/**
 * Consecutive entries that fall on the same day, in the order they arrived.
 *
 * The feed is already newest-first from the server, so grouping is a fold
 * over neighbours rather than a sort — a re-sort here would be the client
 * disagreeing with the cursor it paged with, and the first duplicate day
 * heading would be the symptom.
 *
 * `formatDayHeading` supplies "today", "yesterday" and then the date, all
 * from `Intl`, so this adds no strings to translate. An entry whose
 * timestamp does not parse falls into a group headed by the raw value
 * rather than being dropped: a notification with a bad date is still a
 * notification worth seeing, which is the same rule the row follows for an
 * unknown type.
 */
function groupByDay(
  entries: Notification[],
  locale: string,
  t: (key: TranslationKey, values?: Record<string, string | number>) => string,
): { heading: string; entries: Notification[] }[] {
  const groups: { heading: string; entries: Notification[] }[] = [];

  for (const entry of entries) {
    const heading = formatDayHeading(entry.created_at, locale, t) ?? entry.created_at;
    const last = groups.at(-1);
    if (last !== undefined && last.heading === heading) last.entries.push(entry);
    else groups.push({ heading, entries: [entry] });
  }

  return groups;
}
