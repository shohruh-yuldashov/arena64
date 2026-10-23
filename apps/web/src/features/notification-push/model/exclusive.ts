/**
 * One change to this browser's push subscription at a time — A64-031.E6.
 *
 * ## The race this closes
 *
 * Enabling push is two steps — the browser subscribes, then the backend
 * stores the endpoint for this account — and checking ownership is three:
 * read the subscription, ask the backend, unsubscribe if it is not ours.
 * Interleaved, the check can land between the two enable steps:
 *
 *     enable      subscribes
 *     check       reads the new subscription, asks: not yours (not stored yet)
 *     enable      stores it
 *     check       unsubscribes it
 *
 * and the account is left with a stored device that receives nothing. Run
 * one after the other, the check sees either no subscription or a stored
 * one, and both answers are right.
 *
 * So everything that changes the subscription takes this lock: enabling,
 * disabling (and with it both releases in `release.ts`), and the ownership
 * check in `ownership.ts`.
 *
 * ## Across tabs, where the browser allows it
 *
 * The subscription belongs to the browser profile, so the other half of the
 * race can be in another tab. The Web Locks API is held by name across every
 * tab of this origin, which is exactly that scope. Where it is missing the
 * tasks are still queued within this tab, which covers the one place a
 * person can cause the race themselves — signing in and pressing "enable"
 * on the same page.
 *
 * Every task holding the lock ends: the API client times out every request
 * (`shared/api/client.ts`), and nothing here waits on `serviceWorker.ready`
 * unbounded (see `activeRegistration`).
 */
const LOCK_NAME = "arena64:push-subscription";

let queue: Promise<unknown> = Promise.resolve();

export async function exclusively<T>(task: () => Promise<T>): Promise<T> {
  if ("locks" in navigator) {
    // Awaited for its type: `lib.dom` declares the result as whatever the
    // callback returns — here a promise — where the lock in fact resolves
    // with the value that promise settles to.
    const result = await navigator.locks.request(LOCK_NAME, task);
    return result;
  }

  const run = queue.then(task, task);
  // The queue only orders tasks; a failure belongs to its own caller.
  queue = run.catch(() => undefined);
  return run;
}
