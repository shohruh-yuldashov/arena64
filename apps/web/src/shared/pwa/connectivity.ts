import { useSyncExternalStore } from "react";

/**
 * The browser's opinion about the network — A64-020.9 §20.
 *
 * ## What this is, and what it is emphatically not
 *
 * `navigator.onLine` answers "does this device have *a* network
 * interface", not "is Arena64 reachable". A captive portal, a VPN that has
 * dropped, a backend that is down: all of them report `true`. So this is a
 * **hint**, used for one thing — telling a player that the thing they just
 * noticed is their connection rather than the app.
 *
 * Authority stays where it already is: `shared/realtime`'s connection
 * status for the socket, and a failed request for the API. Neither
 * consults this, and neither should. §20's rule, and the reason the
 * offline notice never says a game is or is not running.
 *
 * `false` is trustworthy in one direction only — the browser knows when it
 * has no interface — which is why the notice appears on `offline` and
 * disappears on `online` rather than trying to verify either.
 */

export function isOnline(): boolean {
  // Assume online where the property is missing. A false offline notice on
  // an unusual runtime is worse than no notice: it tells a player their
  // connection is broken when the only broken thing is the detection.
  if (typeof navigator === "undefined") return true;
  return navigator.onLine !== false;
}

function subscribeToConnectivity(listener: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  window.addEventListener("online", listener);
  window.addEventListener("offline", listener);
  return () => {
    window.removeEventListener("online", listener);
    window.removeEventListener("offline", listener);
  };
}

export function useOnline(): boolean {
  return useSyncExternalStore(subscribeToConnectivity, isOnline, () => true);
}
