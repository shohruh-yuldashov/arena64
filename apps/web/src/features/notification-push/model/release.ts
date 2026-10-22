import { onAccountReplaced, onSessionEnding } from "@/features/auth/model/session-provider";
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
 *
 * ## When the account is replaced without a sign-out — A64-031.E5
 *
 * The refresh cookie is shared by every tab, so a tab can become another
 * account with no sign-out here — and then neither half above runs. The
 * subscription stays bound to the previous account, and its notifications
 * keep arriving on a browser somebody else is now using.
 *
 * Only the browser half can be done then. The previous account's session is
 * gone from this tab, and the server rightly refuses to let the *new* one
 * remove a row it does not own. Unsubscribing in the browser is enough: the
 * push service stops delivering at once, and the stored row answers `410`
 * on its next delivery and is revoked by the worker (§17). The new account
 * is not enrolled — it enables push itself if it wants it.
 *
 * Both handlers need a tab to see the session end. When none did — a
 * session that expired, a browser closed — `ownership.ts` catches the
 * subscription at the next sign-in instead (A64-031.E6).
 */
export function registerPushSessionRelease(): () => void {
  const releaseOnSignOut = onSessionEnding(async () => {
    const endpoint = await disablePush();
    if (endpoint) await removePushSubscription(endpoint);
  });
  const releaseOnReplacement = onAccountReplaced(async () => {
    await disablePush();
  });
  return () => {
    releaseOnSignOut();
    releaseOnReplacement();
  };
}
