import { useQuery } from "@tanstack/react-query";

import { getMe } from "@/features/auth/api";
import { authKeys } from "@/features/auth/api/keys";
import { useSession } from "@/features/auth/model/session-provider";

/**
 * The signed-in account, read from the server — A64-031.E.
 *
 * Not the user held in the session: that copy is whatever the last sign-in
 * or refresh returned, and a settings screen that states a verification
 * status should state the server's current one. One request when the page
 * opens, no polling — nothing on that page changes the address.
 *
 * ## Keyed by who the session says is signed in
 *
 * The refresh cookie is shared by every tab, so a sign-in completed in one
 * tab can make another tab's next refresh answer for a *different*
 * account, with no sign-out and therefore no cache clear. Keyed only as
 * "me", the open page would go on showing the first account's address
 * under the second. With the session's user id in the key, a change of
 * account is a different query: the old answer is never reused, and the
 * card shows loading until the new account's own answer arrives.
 */
export function useAccount() {
  const { state } = useSession();
  const userId = state.status === "authenticated" ? state.user.id : null;
  return useQuery({
    queryKey: authKeys.me(userId),
    queryFn: getMe,
    enabled: userId !== null,
    staleTime: 0,
  });
}
