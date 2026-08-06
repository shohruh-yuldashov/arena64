import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import {
  type NotificationPreferences,
  type PreferenceChange,
  readNotificationPreferences,
  updateNotificationPreferences,
} from "@/features/notification-preferences/api";
import { notificationPreferenceKeys } from "@/features/notification-preferences/api/keys";

/**
 * Reading and saving notification preferences — A64-021.3 §17.
 *
 * ## The server's answer replaces the cache; nothing is guessed
 *
 * `PATCH` returns the whole resolved matrix, so the mutation writes that
 * response straight into the cache instead of invalidating and refetching.
 * One request per save, and the screen shows what the server actually
 * stored — including a value the server *declined* to change, which an
 * optimistic update would have shown as changed.
 *
 * There is deliberately no optimistic update. A toggle that flips and then
 * flips back is worse than one that waits: this is a consent control, and
 * the honest thing is to show what is stored rather than what was asked.
 */
export function useNotificationPreferences() {
  const { state } = useSession();

  return useQuery({
    queryKey: notificationPreferenceKeys.all(),
    queryFn: readNotificationPreferences,
    enabled: isAuthenticated(state),
  });
}

export function useUpdateNotificationPreferences() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (changes: PreferenceChange[]) => updateNotificationPreferences(changes),
    onSuccess: (settings: NotificationPreferences) => {
      queryClient.setQueryData(notificationPreferenceKeys.all(), settings);
    },
  });
}
