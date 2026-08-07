import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import { updateNotificationPreferences } from "@/features/notification-preferences/api";
import { notificationPreferenceKeys } from "@/features/notification-preferences/api/keys";
import {
  readPushStatus,
  registerPushSubscription,
  removePushSubscription,
} from "@/features/notification-push/api";
import { pushKeys } from "@/features/notification-push/api/keys";
import { isPushSupported, pushCapabilities } from "@/shared/pwa";

import { type PushState, pushStateOf } from "./state";
import {
  currentSubscription,
  disablePush,
  type EnableFailure,
  enablePush,
} from "./subscription";

/**
 * The categories this switch turns on and off — A64-021.6A.
 *
 * **Every category the backend can push**, and keeping the two in step is
 * the point: A64-021.6A added the social types to `PUSH_CAPABLE_TYPES` while
 * this list still said `tournament` only, so somebody who pressed "Enable
 * push notifications" was told push was on and then received nothing when a
 * friend request arrived — the switch and the channel disagreeing about what
 * "on" means.
 *
 * Somebody who wants tournament pushes and not social ones has the matrix
 * below this section for exactly that. This control is the **channel**, and
 * a channel switch that enabled part of a channel is the settings-screen lie
 * §6 forbids.
 */
const PUSHABLE_CATEGORIES = ["tournament", "social"] as const;

/**
 * The push settings section's state and its two actions — A64-021.6 §21, §22.
 *
 * ## Why the whole enable flow is one mutation
 *
 * §21 lists five steps — permission, registration, subscribe, POST, enable
 * preference — and warns against a "half-enabled misleading state". The way
 * that happens is a component wiring five independent mutations and
 * rendering something optimistic after the second.
 *
 * So it is one mutation with the steps in order, and the **preference is
 * turned on last**, after the subscription is stored. A preference enabled
 * before a subscription exists tells somebody push is on with nowhere for
 * it to arrive; a subscription stored without the preference costs one
 * unused row and no wrong claim. Only one of those two orderings has a
 * harmless failure.
 *
 * ## Why disabling does both halves
 *
 * §22 asks for the semantics to be decided and stated. Turning the switch
 * off here **removes this device and mutes the channel**, rather than
 * muting alone.
 *
 * The alternative — keeping the subscription for a quick re-enable — was
 * rejected because it leaves a live capability on a device somebody just
 * asked to stop being notified on, and because the "quick" it saves is one
 * permission-free `subscribe()` call that takes milliseconds. What a person
 * means by turning push off on this laptop is that this laptop stops
 * receiving push.
 */
export interface PushSection {
  readonly state: PushState | "loading";
  readonly deviceCount: number;
  readonly busy: boolean;
  readonly failure: EnableFailure | null;
  enable: () => void;
  disable: () => void;
}

export function usePushSection(preferenceEnabled: boolean): PushSection {
  const queryClient = useQueryClient();
  const [subscribed, setSubscribed] = useState(false);
  const [failure, setFailure] = useState<EnableFailure | null>(null);

  const status = useQuery({
    queryKey: pushKeys.status(),
    queryFn: readPushStatus,
    // Only where the browser could act on the answer. A server round trip
    // to learn a VAPID key that a browser without `PushManager` can never
    // use is a request that exists to be discarded.
    enabled: isPushSupported(pushCapabilities()),
  });

  // Asked of the **browser**, never remembered — §8. A person who cleared
  // site data or revoked the permission in another tab must see the truth
  // here, and the only way to guarantee that is to re-read it.
  const refreshSubscription = useCallback(async () => {
    setSubscribed((await currentSubscription()) !== null);
  }, []);

  useEffect(() => {
    void refreshSubscription();
  }, [refreshSubscription]);

  const enable = useMutation({
    mutationFn: async () => {
      const key = status.data?.vapid_public_key;
      if (!key) throw new Error("push is unavailable");

      const result = await enablePush(key);
      if (!result.ok) return result.reason;

      await registerPushSubscription(result.keys);
      // **Last**, and only once the subscription is stored — see above.
      await updateNotificationPreferences(
        PUSHABLE_CATEGORIES.map((category) => ({ category, channel: "push", enabled: true })),
      );
      return null;
    },
    onSuccess: async (reason) => {
      setFailure(reason);
      await reconcile();
    },
    // A network failure between the browser subscribing and the backend
    // storing it: the browser is subscribed and this platform does not know.
    // Reported as `subscribe-failed` rather than left silent, and recovered
    // on the next attempt — registration is an upsert, so re-enabling stores
    // the same endpoint rather than a second one.
    onError: async () => {
      setFailure("subscribe-failed");
      await reconcile();
    },
  });

  const disable = useMutation({
    mutationFn: async () => {
      const endpoint = await disablePush();
      // The backend first would leave a live browser subscription pointing
      // at a revoked row — harmless, but it means a re-enable sees an
      // existing subscription and skips the `subscribe()` that would have
      // refreshed it. Browser first, then the record.
      if (endpoint) await removePushSubscription(endpoint);
      await updateNotificationPreferences(
        PUSHABLE_CATEGORIES.map((category) => ({ category, channel: "push", enabled: false })),
      );
    },
    onSettled: async () => {
      setFailure(null);
      await reconcile();
    },
  });

  /** Re-reads every authority after an action — §21's "reconcile". */
  async function reconcile(): Promise<void> {
    await refreshSubscription();
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: pushKeys.status() }),
      queryClient.invalidateQueries({ queryKey: notificationPreferenceKeys.all() }),
    ]);
  }

  return {
    state: pushStateOf({ status: status.data, subscribed, preferenceEnabled }),
    deviceCount: status.data?.device_count ?? 0,
    busy: enable.isPending || disable.isPending,
    failure,
    enable: () => {
      setFailure(null);
      enable.mutate();
    },
    disable: () => disable.mutate(),
  };
}
