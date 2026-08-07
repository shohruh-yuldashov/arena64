import { type ReactNode, useEffect } from "react";

import { registerPushSessionRelease } from "@/features/notification-push/model/release";

/**
 * Registers push's sign-out release — A64-021.6 §23.
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
 * Inside `SessionProvider`, because `onSessionEnding` is that module's.
 */
export function PushReleaseProvider({ children }: { children: ReactNode }) {
  useEffect(() => registerPushSessionRelease(), []);
  return <>{children}</>;
}
