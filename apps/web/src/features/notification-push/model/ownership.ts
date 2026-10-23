import { checkPushOwnership } from "@/features/notification-push/api";
import { ApiError } from "@/shared/api";
import { reportError } from "@/shared/lib/report-error";

import { exclusively } from "./exclusive";
import { currentSubscription } from "./subscription";

/**
 * Lets go of a push subscription the signed-in account does not own —
 * A64-031.E6.
 *
 * ## The leak this closes
 *
 * `release.ts` unsubscribes this browser when somebody signs out, and when a
 * tab watches one account replace another. Neither runs when nobody was
 * watching:
 *
 *     A enables push; A's session expires, or the browser is closed
 *     B signs in on the same browser
 *     A's notifications keep arriving on B's screen
 *
 * So whenever an account becomes signed in, the browser asks the backend
 * whether the subscription it holds is that account's, and unsubscribes it
 * if not. Unsubscribing in the browser is all that is needed and all that
 * is allowed: the push service stops delivering at once, and the other
 * account's row answers `410` on its next delivery and is revoked by the
 * worker (§17). That row is never touched from here — removal is scoped to
 * its owner, and the owner is not signed in.
 *
 * ## Why the server decides, and nothing is stored here
 *
 * A marker in `localStorage` naming the account that subscribed would be a
 * claim this page wrote about itself, out of step the moment the backend
 * re-binds or revokes the row, and absent for every subscription made before
 * it existed. The backend's record is the only thing that says whose
 * notifications reach this browser, so it is the only thing asked — and
 * `subscription.ts` already keeps nothing, for the same reason.
 *
 * ## Failing closed
 *
 * A check that fails is treated as "not yours": a missed notification on
 * the owner's own browser is recoverable by enabling push again, and
 * another account's notifications on somebody else's screen is not. The
 * one exception is a request the session cancelled because the account
 * changed while it was in flight — the question no longer applies, and the
 * account now signed in asks its own. The owner's other devices are never
 * affected: only this browser's subscription is let go.
 */
export async function reconcilePushOwnership(isCurrent: () => boolean): Promise<void> {
  await exclusively(async () => {
    // Queued behind another tab's enable, and the account may have changed
    // while it waited. Whoever is signed in now has their own check queued.
    if (!isCurrent()) return;

    const subscription = await currentSubscription();
    if (!subscription) return;
    if ((await ownershipOf(subscription.endpoint)) !== false) return;

    if (!(await subscription.unsubscribe())) {
      throw new Error("The browser refused to release a push subscription.");
    }
    for (const listener of releaseListeners) listener();
  });
}

/**
 * Who must hear that the check let the subscription go — the push settings
 * section, when it is mounted.
 *
 * It reads the browser once, on mount, and a page loaded straight onto it
 * reads before the check has answered: without this it would go on saying
 * push is on here, offering "turn off" instead of the enable that recovers.
 */
const releaseListeners = new Set<() => void>();

export function onOwnershipRelease(listener: () => void): () => void {
  releaseListeners.add(listener);
  return () => {
    releaseListeners.delete(listener);
  };
}

/** Whether the endpoint is the signed-in account's, or `null` if nobody asked any more. */
async function ownershipOf(endpoint: string): Promise<boolean | null> {
  try {
    return (await checkPushOwnership(endpoint)).owned;
  } catch (error) {
    if (error instanceof ApiError && error.kind === "canceled") return null;
    reportError(error, { scope: "push-ownership" });
    return false;
  }
}
