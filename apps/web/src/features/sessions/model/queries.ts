import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { readSessions, revokeSession } from "@/features/sessions/api";
import { sessionKeys } from "@/features/sessions/api/keys";

/**
 * Reading and revoking devices — A64-030.5C, SE-2.
 *
 * ## Why revoking invalidates rather than updating the cache
 *
 * `DELETE` answers `204` with no body, so there is nothing to write in.
 * Removing the row locally would be an optimistic update on a **security**
 * screen, and the failure mode is the one that must not happen: a device
 * that quietly reappears on the next refetch, having never been signed out,
 * after somebody was shown that it had been.
 *
 * A refetch is one request and shows what the server actually did.
 */
export function useSessions() {
  const { state } = useSession();

  return useQuery({
    queryKey: sessionKeys.all(),
    queryFn: readSessions,
    enabled: isAuthenticated(state),
  });
}

export function useRevokeSession() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (id: string) => revokeSession(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sessionKeys.all() }),
  });
}
