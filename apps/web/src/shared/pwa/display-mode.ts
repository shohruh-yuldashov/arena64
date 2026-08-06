import { useSyncExternalStore } from "react";

/**
 * Where the application is running, and on what — A64-020.9 §17, §18.
 *
 * Two questions, and both are asked for one narrow purpose: deciding what
 * to *say* about installing. Neither is allowed to change routing, guards,
 * or how anything is fetched (§18) — an installed Arena64 is the same
 * application in a different window, and a second code path for it would
 * be a second set of bugs.
 */

const STANDALONE_QUERY = "(display-mode: standalone)";

/**
 * Whether this window is an installed application rather than a tab.
 *
 * Two checks because two platforms answer differently: the media query is
 * the standard, and `navigator.standalone` is Safari's, which predates it
 * and is still the only truthful answer on iOS.
 */
export function isStandaloneDisplay(): boolean {
  if (typeof window === "undefined") return false;
  const iosStandalone = (navigator as { standalone?: boolean }).standalone === true;
  return iosStandalone || window.matchMedia(STANDALONE_QUERY).matches;
}

/**
 * Re-renders if the display mode changes — which it does when a player
 * launches the installed app from a browser tab's session, and when the
 * install completes in some browsers.
 */
export function useStandaloneDisplay(): boolean {
  return useSyncExternalStore(subscribeToDisplayMode, isStandaloneDisplay, () => false);
}

function subscribeToDisplayMode(listener: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  const query = window.matchMedia(STANDALONE_QUERY);
  query.addEventListener("change", listener);
  return () => query.removeEventListener("change", listener);
}

/**
 * Whether this is Safari on iOS — the one platform with no install prompt.
 *
 * §17 asks to *"detect only enough to show truthful guidance"*, and this is
 * that: two regular expressions rather than a user-agent database, because
 * the only decision downstream is whether to render a sentence explaining
 * the Share menu.
 *
 * `Macintosh` with touch points is iPadOS, which has reported itself as a
 * Mac since iPadOS 13 and would otherwise never see the guidance it is the
 * likeliest device to need. The second test excludes the iOS browsers that
 * are Safari underneath but put their own menu in front of the user, where
 * "Share, then Add to Home Screen" would be the wrong instructions.
 */
export function isIosSafari(): boolean {
  if (typeof navigator === "undefined") return false;
  const agent = navigator.userAgent;
  const isIos =
    /iPad|iPhone|iPod/.test(agent) ||
    (agent.includes("Macintosh") && navigator.maxTouchPoints > 1);
  if (!isIos) return false;
  return !/CriOS|FxiOS|EdgiOS|OPiOS/.test(agent);
}
