import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";

import { notificationKeys } from "@/features/notifications/api/keys";
import { type InboundFrame, useFrames } from "@/shared/realtime";

/**
 * Notifications, pushed — A64-021.2 §4, §5, §6.
 *
 * A64-021.1 refetched the unread count when the tab regained focus, because
 * nothing put a notification on a socket. Something does now, and this is
 * the client half: `notification.created` arrives on the **one shared
 * socket** and the badge stops waiting for a focus event.
 *
 * ## It invalidates. It does not render, and it does not decide
 *
 * §5: receiving a frame must never mutate the UI directly. This hook reads
 * exactly one field — the id, to tell news from a duplicate — and then
 * invalidates. HTTP decides everything else: whether the notification is
 * still there, whether it is unread, and what it says.
 *
 * That is what makes a **late frame harmless**. A player who read a
 * notification on their phone and then receives its push on their laptop
 * gets an invalidation, and the refetch says the count is unchanged. A hook
 * that incremented a badge would reopen it, which is precisely the failure
 * §5 names.
 *
 * ## Duplicates collapse into one refetch
 *
 * A reconnect can replay several frames at once, and three pushes must not
 * be three pairs of `GET`s. Two guards, because they answer different
 * questions:
 *
 *     seen       *this* notification was already reconciled — a genuine
 *                duplicate, dropped for nothing
 *     inFlight   several *different* notifications arrived together; one
 *                read answers all of them
 *
 * Both are bounded. `seen` is a ring rather than a growing set: a long
 * session would otherwise accumulate every id it has ever been told about
 * to answer a question only the last few can be asked.
 *
 * ## Polling is not removed — §6
 *
 * The badge still refetches on focus and the list still refetches on its own
 * terms. Realtime reduces latency; it is not a second source of truth, and a
 * socket that never connects leaves the product exactly as A64-021.1 shipped
 * it.
 *
 * ## Where it is mounted
 *
 * `AppShell`, so it is alive on every route rather than only where a
 * notification surface happens to be rendered — unlike `useMatchOfferPush`,
 * which the lobby mounts because a match offer has nowhere else to go. A
 * notification's destination is the badge in the header, and the header is
 * always there.
 *
 * Not a provider: §4 forbids a second connection and there is nothing to
 * provide. `useFrames` subscribes to the socket `app/providers` already
 * owns.
 */

/** Ids already reconciled, bounded so a long session cannot grow it. */
const REMEMBERED = 32;

export function useNotificationPush(): void {
  const client = useQueryClient();

  const seen = useRef<string[]>([]);
  const inFlight = useRef(false);

  useFrames(
    useCallback(
      (frame: InboundFrame) => {
        if (frame.type !== "notification.created") return;

        const notificationId = frame.payload.notification_id;
        // A frame whose id is not a string is not one this build can
        // deduplicate, and acting on it would defeat the guard. Dropped
        // rather than reconciled — the next focus refetch still catches the
        // notification, because the read is the source of truth.
        if (typeof notificationId !== "string") return;

        if (seen.current.includes(notificationId)) return;
        seen.current = [...seen.current, notificationId].slice(-REMEMBERED);

        if (inFlight.current) return;
        inFlight.current = true;

        // **Both keys, and only these two** — §4. The list holds the rows
        // and the count holds the badge; they are separate queries with
        // separate costs, and invalidating one would leave the header and
        // the page disagreeing about the same notification.
        //
        // Nothing else is touched. A notification is not a friend request,
        // not a match and not a profile, and invalidating those on a
        // notification would make one push a refetch storm.
        void Promise.all([
          client.invalidateQueries({ queryKey: notificationKeys.list() }),
          client.invalidateQueries({ queryKey: notificationKeys.unreadCount() }),
        ]).finally(() => {
          inFlight.current = false;
        });
      },
      [client],
    ),
  );
}
