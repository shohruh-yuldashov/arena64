import { useEffect, useSyncExternalStore } from "react";

/**
 * The one reason an application update waits — A64-020.9 §14.
 *
 * ## The problem this solves
 *
 * Activating a new service worker reloads the page. A reload during a live
 * game is a resignation on the clock; during a pending match offer it is a
 * declined offer; during a registration mutation it is an unanswered
 * request whose outcome nobody sees. §14 names five such moments, and none
 * of them is knowable from `shared/`.
 *
 * So the surfaces that *do* know publish it here. A component holds the
 * update for as long as it is mounted in an unsafe state, and releases it
 * when it is not — `useHoldAppUpdate(active)` is the whole interface.
 *
 * ## Why a counter and not a set of reasons
 *
 * Two surfaces can be unsafe at once — a game running while a tournament
 * registration settles — and the answer to "may we reload?" is the same
 * either way. A set of reasons would only be worth its complexity if the
 * UI said *which* one, and it deliberately does not: "after the current
 * game" is the message a player needs, not a list of internal states.
 *
 * ## This is not the only guard
 *
 * The update prompt never reloads on its own in the first place — the user
 * presses Update (§14). This exists so that the button is not *offered*
 * while pressing it would cost something, and so that `applyAppUpdate`
 * refuses even if some future caller forgets to check.
 */

let holds = 0;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

/**
 * Holds the update until the returned function is called.
 *
 * Imperative, so a non-React caller (a mutation callback, a socket
 * handler) can use it. React callers want `useHoldAppUpdate`.
 */
export function holdAppUpdate(): () => void {
  holds += 1;
  notify();

  let released = false;
  return () => {
    // Guarded: a caller that released twice would decrement somebody
    // else's hold, and the symptom would be a reload during a game with
    // nothing in the code to point at.
    if (released) return;
    released = true;
    holds -= 1;
    notify();
  };
}

export function isAppUpdateHeld(): boolean {
  return holds > 0;
}

export function subscribeToAppUpdateHold(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Re-renders when the hold changes. */
export function useAppUpdateHeld(): boolean {
  return useSyncExternalStore(subscribeToAppUpdateHold, isAppUpdateHeld, () => false);
}

/**
 * Holds the update while `active` is true, for as long as the component
 * is mounted.
 *
 * The effect's cleanup is what makes this safe under every unmount path,
 * including the one nobody thinks about: a player navigating away from a
 * game mid-move. A hold released by hand in an event handler would leak on
 * that path and block updates for the rest of the session.
 */
export function useHoldAppUpdate(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    return holdAppUpdate();
  }, [active]);
}
