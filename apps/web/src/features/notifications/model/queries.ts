import {
  type InfiniteData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useRef } from "react";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import {
  markAllNotificationsRead,
  markNotificationRead,
  type NotificationPage,
  readNotifications,
  readUnreadCount,
} from "@/features/notifications/api";
import { notificationKeys } from "@/features/notifications/api/keys";

/**
 * Reading notifications and marking them read — A64-021.1 §19, §21, §24.
 *
 * ## Delivery is a poll, and this phase says so
 *
 * A64-021.2 pushes notifications over the socket that already exists. Until
 * then the badge is a **query that refetches when the tab regains focus**,
 * which is the platform's global default (`shared/api/query-client.ts`) and
 * the cheapest honest approximation: a player who comes back to the tab sees
 * a current count, and one who leaves it open does not.
 *
 * There is deliberately **no interval**. A poll would be a request per
 * player per tick for a number that changes at social-event frequency, and
 * §24 forbids claiming realtime delivery that does not exist. This is the
 * temporary policy, recorded here so its replacement is a deletion.
 */

/** Twenty fills a viewport with room to scroll, which is the signal that
 *  "load more" exists. The endpoint caps at fifty. */
export const NOTIFICATION_PAGE_SIZE = 20;

/**
 * How long a count is trusted before a focus event refetches it.
 *
 * Shorter than the platform's thirty seconds, because this is the one read
 * whose whole purpose is to be current and whose cost is a single indexed
 * `COUNT`. Long enough that switching tabs twice in a row is one request.
 */
const UNREAD_STALE_TIME_MS = 10_000;

export function useNotifications() {
  const { state } = useSession();

  return useInfiniteQuery({
    queryKey: notificationKeys.list(),
    queryFn: ({ pageParam }) =>
      readNotifications({ after: pageParam, limit: NOTIFICATION_PAGE_SIZE }),
    initialPageParam: null as string | null,
    // `undefined` is what TanStack reads as "no more"; the API sends `null`.
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: isAuthenticated(state),
  });
}

export function useUnreadCount() {
  const { state } = useSession();

  return useQuery({
    queryKey: notificationKeys.unreadCount(),
    queryFn: readUnreadCount,
    enabled: isAuthenticated(state),
    staleTime: UNREAD_STALE_TIME_MS,
    // A count that could not be fetched is **not** an error state worth
    // showing: §22 forbids a permanent red badge because one request failed.
    // The badge simply renders nothing until a later attempt succeeds.
    retry: false,
  });
}

/**
 * Marks one notification read, at most once per notification.
 *
 * ## Why a ref of in-flight ids rather than `isPending`
 *
 * `isPending` is a property of the *mutation*, not of a notification, so it
 * cannot tell a second click on the same row from a first click on another.
 * A player tapping two notifications in quick succession must send two
 * requests; tapping one twice must send one (§21).
 *
 * The server is idempotent either way — this exists so the client does not
 * rely on that, and so a double tap does not decrement a badge twice.
 *
 * ## Never awaited by the caller
 *
 * The row navigates immediately and lets this settle on its own. §21:
 * navigation must not block on a mutation, because a notification opened on
 * a bad connection would otherwise be a tap that appears to do nothing.
 */
export function useMarkNotificationRead() {
  const queryClient = useQueryClient();
  const inFlight = useRef(new Set<string>());

  const mutation = useMutation({
    mutationFn: markNotificationRead,
    onSuccess: (result, notificationId) => {
      // Patched in place rather than by invalidating the list: invalidating
      // an infinite query refetches **every** page it holds, which is a
      // handful of requests to change one boolean.
      queryClient.setQueryData<InfiniteData<NotificationPage>>(
        notificationKeys.list(),
        (data) => markReadInPages(data, notificationId, result.marked_read > 0),
      );
      // The badge is reconciled with the server rather than decremented
      // here — one cheap request, and it stays correct if another device
      // read something in the meantime (§21).
      void queryClient.invalidateQueries({ queryKey: notificationKeys.unreadCount() });
    },
    onSettled: (_result, _error, notificationId) => {
      inFlight.current.delete(notificationId);
    },
  });

  const markRead = useCallback(
    (notificationId: string, isRead: boolean) => {
      if (isRead || inFlight.current.has(notificationId)) return;
      inFlight.current.add(notificationId);
      mutation.mutate(notificationId);
    },
    [mutation],
  );

  return { markRead, error: mutation.error };
}

/**
 * Marks everything unread read.
 *
 * The list is patched and the count invalidated, in that order, so the rows
 * and the badge never disagree mid-flight. The caller disables its control
 * on `isPending` (§21) — this hook does not hide the button itself, because
 * where a control lives is the widget's decision.
 */
export function useMarkAllNotificationsRead() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: markAllNotificationsRead,
    onSuccess: () => {
      queryClient.setQueryData<InfiniteData<NotificationPage>>(
        notificationKeys.list(),
        markEveryPageRead,
      );
      void queryClient.invalidateQueries({ queryKey: notificationKeys.unreadCount() });
    },
  });
}

/**
 * One notification marked read, across whichever page holds it.
 *
 * `read_at` is set to the client's clock only when the server said this
 * call is what changed it — and only for display. The authoritative instant
 * is the server's, and the next refetch replaces this one (§23).
 */
function markReadInPages(
  data: InfiniteData<NotificationPage> | undefined,
  notificationId: string,
  changed: boolean,
): InfiniteData<NotificationPage> | undefined {
  if (data === undefined || !changed) return data;
  return {
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      entries: page.entries.map((entry) =>
        entry.id === notificationId
          ? { ...entry, is_read: true, read_at: entry.read_at ?? new Date().toISOString() }
          : entry,
      ),
    })),
  };
}

function markEveryPageRead(
  data: InfiniteData<NotificationPage> | undefined,
): InfiniteData<NotificationPage> | undefined {
  if (data === undefined) return data;
  const now = new Date().toISOString();
  return {
    ...data,
    pages: data.pages.map((page) => ({
      ...page,
      entries: page.entries.map((entry) =>
        entry.is_read ? entry : { ...entry, is_read: true, read_at: now },
      ),
    })),
  };
}
