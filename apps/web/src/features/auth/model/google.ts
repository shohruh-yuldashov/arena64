import { useQuery } from "@tanstack/react-query";

import { googlePending } from "@/features/auth/api";
import { authKeys } from "@/features/auth/api/keys";

/**
 * The step this browser's Google sign-in is waiting for — A64-031.E1.
 *
 * Asked once per page: the answer changes only when the person completes
 * the step, which navigates away. No retry, because the one expected
 * failure — `401 external_sign_in_expired` — is an answer, not a fault.
 */
export function useGooglePending() {
  return useQuery({
    queryKey: authKeys.googlePending(),
    queryFn: googlePending,
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
}
