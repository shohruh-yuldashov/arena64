/**
 * The extension point A64-021 Notifications will build on — A64-020.9 §21.
 *
 * ## What this is
 *
 * Capability detection, and nothing else. It reports what the browser can
 * do; it does not ask the browser to do any of it.
 *
 * ## What this deliberately is not
 *
 * No permission request, no `PushManager.subscribe`, no VAPID key, no
 * device registration, no notification. Every one of those is a
 * Notifications decision — who may be notified about what, how a
 * subscription is revoked, what a device row contains — and answering them
 * here would settle a domain's design inside a PWA phase (§21).
 *
 * ## What A64-021 has to add
 *
 * | Piece | Where |
 * | --- | --- |
 * | Ask for permission, at a moment the user has asked to be notified | a Notifications feature, never on load |
 * | `registration.pushManager.subscribe({ applicationServerKey })` | beside the permission request |
 * | The VAPID public key, as a `VITE_`-prefixed variable | `shared/config/env.ts` |
 * | A `push` handler and a `notificationclick` handler | `pwa/service-worker.ts` — the same worker, not a second one |
 * | The device/subscription store and its endpoints | `apps/api` |
 *
 * The worker's message contract stays as narrow as it is now (§31): a push
 * handler reacts to a *push* event, and adding one must not widen what a
 * page is allowed to tell the worker to do.
 */

export interface PushCapabilities {
  /** A worker can exist at all — the prerequisite for the other two. */
  readonly serviceWorker: boolean;
  readonly pushManager: boolean;
  readonly notifications: boolean;
}

export function pushCapabilities(): PushCapabilities {
  const hasWindow = typeof window !== "undefined";
  return {
    serviceWorker: typeof navigator !== "undefined" && "serviceWorker" in navigator,
    pushManager: hasWindow && "PushManager" in window,
    notifications: hasWindow && "Notification" in window,
  };
}

/** All three, because push delivery needs all three. */
export function isPushSupported(capabilities: PushCapabilities = pushCapabilities()): boolean {
  return capabilities.serviceWorker && capabilities.pushManager && capabilities.notifications;
}
