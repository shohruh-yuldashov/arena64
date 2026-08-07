import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef } from "react";

import { invalidateChallenges } from "@/features/challenges/model/queries";
import { type InboundFrame, useFrames } from "@/shared/realtime";

/**
 * Challenges, kept fresh by the notification frame — A64-022.5 §11.
 *
 * ## No new protocol, and none was needed
 *
 * §11 forbids a challenge websocket and A64-022.4's audit already explained
 * why one would be redundant: `friend_challenge_received` and
 * `friend_challenge_accepted` are notifications, they arrive on the one
 * shared socket as `notification.created`, and a notification frame says
 * *something happened* — which is exactly the trigger a list that re-reads
 * over HTTP needs.
 *
 * So this hook reads **one field** of the frame, the notification type, and
 * invalidates. It never renders and never decides: whether the challenge is
 * still there, whether it expired, and who it is from are all answered by
 * the read.
 *
 * That is what makes a late or duplicated frame harmless. A player who
 * declined a challenge on their phone and then receives its frame on their
 * laptop gets an invalidation, and the refetch says the list is unchanged.
 *
 * ## Both lists, on either type
 *
 * The frame carries a type but not a side, and both types can move both
 * lists: an acceptance removes a row from the challenger's **outgoing**
 * list, and a creation adds one to the recipient's **incoming**. Guessing
 * from the type would be right today and wrong the first time a lifecycle
 * type is added — and the saving is one request on a page that shows both.
 *
 * ## Why it does not duplicate `useNotificationPush`
 *
 * That hook invalidates the notification list and the badge; this one
 * invalidates the challenge lists. They subscribe to the same frame through
 * `useFrames` — which is a fan-out over the one socket `app/providers`
 * owns, not a second connection — and neither knows about the other's keys.
 *
 * Mounted by the challenge page rather than by `AppShell`, deliberately: a
 * challenge list that is not on screen has no cache worth refreshing, and
 * the badge already tells a player on another route that something arrived.
 */

/** Ids already reconciled, bounded so a long session cannot grow it. */
const REMEMBERED = 16;

/** The notification types that can change either challenge list. */
const CHALLENGE_TYPES = new Set(["friend_challenge_received", "friend_challenge_accepted"]);

export function useChallengePush(): void {
  const client = useQueryClient();

  const seen = useRef<string[]>([]);
  const inFlight = useRef(false);

  useFrames(
    useCallback(
      (frame: InboundFrame) => {
        if (frame.type !== "notification.created") return;
        if (typeof frame.payload.type !== "string") return;
        if (!CHALLENGE_TYPES.has(frame.payload.type)) return;

        const notificationId = frame.payload.notification_id;
        // Not deduplicable, so not acted on. The list still recovers on its
        // own terms, because the read is the source of truth.
        if (typeof notificationId !== "string") return;

        if (seen.current.includes(notificationId)) return;
        seen.current = [...seen.current, notificationId].slice(-REMEMBERED);

        // Single-flight: a reconnect replaying several frames collapses
        // into one pair of reads, because one pair answers all of them.
        if (inFlight.current) return;
        inFlight.current = true;

        void Promise.resolve(invalidateChallenges(client)).finally(() => {
          inFlight.current = false;
        });
      },
      [client],
    ),
  );
}
