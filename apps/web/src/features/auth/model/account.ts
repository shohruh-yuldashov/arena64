import { useQuery } from "@tanstack/react-query";

import { getMe } from "@/features/auth/api";
import { authKeys } from "@/features/auth/api/keys";

/**
 * The signed-in account, read from the server — A64-031.E.
 *
 * Not the user held in the session: that copy is whatever the last sign-in
 * or refresh returned, and a settings screen that states a verification
 * status should state the server's current one. One request when the page
 * opens, no polling — nothing on that page changes the address.
 */
export function useAccount() {
  return useQuery({
    queryKey: authKeys.me(),
    queryFn: getMe,
    staleTime: 0,
  });
}
