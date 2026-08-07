import { onSessionEnding } from "@/features/auth/model/session-provider";
import { removePushSubscription } from "@/features/notification-push/api";

import { disablePush } from "./subscription";

/**
 * Removes this browser's push subscription when a session ends — §23.
 *
 * ## The leak this closes
 *
 * A push subscription belongs to the **browser profile**, not to a tab or a
 * session. It survives a sign-out, a tab close and a restart. So without
 * this, a shared laptop looks like:
 *
 *     A signs in, enables push, signs out
 *     B signs in on the same browser
 *     A's tournament notifications arrive on B's screen
 *
 * The endpoint upsert (`§23`, the backend's unique constraint) closes the
 * half where B *also* enables push — the endpoint is re-bound and A loses
 * it. This closes the half where B never does: nothing re-binds the
 * endpoint, so it stays A's, and the delivery worker keeps pushing.
 *
 * ## Browser first, then the record
 *
 * `disablePush()` unsubscribes at the browser and returns the endpoint,
 * which is then revoked server-side. The other order would leave a live
 * browser subscription for a revoked row: harmless for delivery — the push
 * service answers and nothing is stored to send — but it means the *next*
 * enable finds an existing subscription and skips the `subscribe()` call
 * that would have re-registered it.
 *
 * ## Best effort, always
 *
 * Registered through `onSessionEnding`, which runs every release with
 * `allSettled`: a network failure here must not stop somebody signing out.
 * The recovery is automatic — the browser is already unsubscribed, so the
 * stored row answers `410` on its next delivery and is revoked by the
 * worker (§17).
 */
export function registerPushSessionRelease(): () => void {
  return onSessionEnding(async () => {
    const endpoint = await disablePush();
    if (endpoint) await removePushSubscription(endpoint);
  });
}
