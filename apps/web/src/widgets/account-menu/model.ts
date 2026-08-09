import { useState } from "react";

import { useSession } from "@/features/auth/model/session-provider";

/**
 * Signing out, with the in-flight flag the two surfaces that offer it both
 * need — A64-025.3 §9, §15.
 *
 * Shared because it is genuinely shared: the header offers sign-out on a
 * wide screen and the mobile panel offers it on a narrow one, and they are
 * never both mounted. Copying ten lines of state into the second one is how
 * the two stop agreeing about what a failed sign-out means.
 *
 * **The semantics are unchanged from `SessionMenu`,** deliberately: `signOut`
 * clears this device *before* it rethrows, so by the time the catch runs the
 * user is already signed out and the throw only reports that the server was
 * not reached. There is nothing to do but stop showing a spinner — swallowing
 * it here is not a silent failure, it is the documented contract of the call.
 */
export function useSignOutAction(): { signingOut: boolean; signOut: () => Promise<void> } {
  const { signOut } = useSession();
  const [signingOut, setSigningOut] = useState(false);

  return {
    signingOut,
    signOut: async () => {
      setSigningOut(true);
      try {
        await signOut();
      } catch {
        // See the docstring: the device is already clear.
      } finally {
        setSigningOut(false);
      }
    },
  };
}
