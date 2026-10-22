import { type ReactNode, useEffect } from "react";

import { useSession } from "@/features/auth/model/session-provider";
import { reconcilePushOwnership } from "@/features/notification-push/model/ownership";
import { registerPushSessionRelease } from "@/features/notification-push/model/release";
import { reportError } from "@/shared/lib/report-error";

/**
 * Registers push's sign-out release — A64-021.6 §23 — and checks this
 * browser's subscription whenever an account becomes signed in —
 * A64-031.E6.
 *
 * A component in the `app` layer, for the reason `RealtimeProvider` is one:
 * `features/auth` publishes `onSessionEnding` precisely so that it does not
 * have to know push exists, and something has to introduce the two. The
 * `app` layer is where features are allowed to meet.
 *
 * Renders nothing and holds no state. It exists so that the registration is
 * tied to the application's lifetime rather than to whether somebody
 * happens to have `/settings/notifications` open — the leak it closes
 * happens at sign-out, which can be initiated from any page.
 *
 * The ownership check runs once per signed-in account, keyed by its id: a
 * page load, a sign-in, and an account replacing another each start one;
 * a token refresh for the same account does not. A browser with no
 * subscription makes no request — see `model/ownership.ts`.
 *
 * Inside `SessionProvider`, because `onSessionEnding` is that module's.
 */
export function PushReleaseProvider({ children }: { children: ReactNode }) {
  const { state } = useSession();
  const signedInAs = state.status === "authenticated" ? state.user.id : null;

  useEffect(() => registerPushSessionRelease(), []);

  useEffect(() => {
    if (signedInAs === null) return;
    let isCurrent = true;
    reconcilePushOwnership(() => isCurrent).catch((error: unknown) => {
      reportError(error, { scope: "push-ownership" });
    });
    return () => {
      isCurrent = false;
    };
  }, [signedInAs]);

  return <>{children}</>;
}
