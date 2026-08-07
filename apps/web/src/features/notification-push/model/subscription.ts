import { isPushSupported, pushCapabilities } from "@/shared/pwa";

/**
 * Talking to the browser's Push API — A64-021.6 §7, §8, §21, §22.
 *
 * Every function here is a thin, testable wrapper over one platform call.
 * They are separate from the React layer for the reason the cache policy is
 * separate from the service worker: these are the decisions that can be
 * wrong, and a `useState` around them is not where a test should have to
 * reach.
 *
 * ## Nothing here is authoritative — §8
 *
 * The browser's `PushManager` holds the subscription and the backend holds
 * the record of it. This module reads both and stores neither: there is no
 * `localStorage` key, no module-level cache, and no "we think we are
 * subscribed" flag. A person who cleared site data, revoked the permission
 * in browser settings, or used a different profile must see the truth on
 * the next render, and the only way to guarantee that is not to remember
 * anything.
 *
 * ## Permission is asked for, never volunteered — §7
 *
 * No function here runs on load. `Notification.requestPermission()` is
 * reachable only from `enablePush`, which is reachable only from a button
 * somebody pressed. A prompt on first page load is the single most reliable
 * way to have a permission denied permanently, and a denied permission
 * cannot be re-requested — it has to be changed in browser settings, which
 * nobody does.
 */

/** The three states the browser distinguishes, and this must too — §7. */
export type PermissionState = "granted" | "denied" | "default";

export function permissionState(): PermissionState {
  if (typeof Notification === "undefined") return "denied";
  return Notification.permission;
}

/**
 * The VAPID public key, as `pushManager.subscribe` wants it.
 *
 * The API takes a `BufferSource` and the server sends base64url, so this
 * conversion is unavoidable. It is a named function rather than three lines
 * inline because getting it wrong produces a subscription bound to the
 * wrong key — which fails at *delivery*, silently, weeks later.
 *
 * `atob` needs the standard alphabet and padding; the server sends url-safe
 * and unpadded, which is what the two replacements and the padding are for.
 */
export function applicationServerKey(base64Url: string): ArrayBuffer {
  const padded = base64Url.padEnd(base64Url.length + ((4 - (base64Url.length % 4)) % 4), "=");
  const binary = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  // The **buffer**, not the view. `PushSubscriptionOptions` takes a
  // `BufferSource` typed as backed by an `ArrayBuffer`, and a `Uint8Array`
  // is generic over `ArrayBufferLike` — which includes `SharedArrayBuffer`
  // and therefore does not satisfy it.
  return bytes.buffer;
}

/** The wire form of a browser subscription — exactly what the API accepts. */
export interface SubscriptionKeys {
  readonly endpoint: string;
  readonly p256dh: string;
  readonly auth: string;
}

/**
 * Reads the three values off a `PushSubscription`.
 *
 * Returns `null` when a key is missing, which the specification permits and
 * which happens on browsers that expose a subscription without exposing its
 * keys. Sending a subscription with an empty key would store one that can
 * never be encrypted to — a device that looks registered and receives
 * nothing.
 */
export function serialize(subscription: PushSubscription): SubscriptionKeys | null {
  const p256dh = subscription.getKey("p256dh");
  const auth = subscription.getKey("auth");
  if (!p256dh || !auth) return null;
  return {
    endpoint: subscription.endpoint,
    p256dh: toBase64Url(p256dh),
    auth: toBase64Url(auth),
  };
}

function toBase64Url(buffer: ArrayBuffer): string {
  const binary = String.fromCharCode(...new Uint8Array(buffer));
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * This browser's current subscription, or `null`.
 *
 * Asked of the **browser**, every time. See the module docstring on why
 * nothing is remembered between calls.
 *
 * ## `getRegistration()`, never `ready`
 *
 * `navigator.serviceWorker.ready` resolves when a worker becomes active —
 * and **never settles at all** when none is registered. It has no rejection
 * path and no timeout.
 *
 * That is a hang, not a slow read, and it is reachable in ordinary use: a
 * first visit before the worker installs, a browser with service workers
 * disabled, or any context the worker was never registered in. This
 * function is awaited on the sign-out path (`model/release.ts`), so an
 * unsettled promise there would leave somebody who pressed "sign out"
 * looking at a spinner forever.
 *
 * `getRegistration()` resolves to `undefined` instead, which is an answer.
 */
export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!isPushSupported(pushCapabilities())) return null;
  const registration = await navigator.serviceWorker.getRegistration();
  if (!registration) return null;
  return registration.pushManager.getSubscription();
}

/** Why enabling push did not work, as a stable code the UI translates. */
export type EnableFailure = "unsupported" | "denied" | "no-service-worker" | "subscribe-failed";

/**
 * How long to wait for a registered worker to become active.
 *
 * Only reached when a registration exists and is still installing — a first
 * visit, or the moments after an update. Installing takes well under a
 * second in practice; five is generous enough never to fire on a slow
 * machine and short enough that somebody watching a spinner gets an answer.
 */
const ACTIVATION_TIMEOUT_MS = 5_000;

/**
 * The active worker, or `null` — **never a promise that does not settle**.
 *
 * ## The defect this exists for
 *
 * `navigator.serviceWorker.ready` resolves when a worker becomes active and
 * **never settles at all** when none is registered. No rejection, no
 * timeout. `enablePush` awaited it directly, so on any page without a
 * registration the button spun forever: no error, no state change, nothing
 * in the console.
 *
 * That is reachable in ordinary use and was reached immediately:
 *
 *     the dev server         `registerServiceWorker` is gated on
 *                            `import.meta.env.PROD`, so `npm run dev` has no
 *                            worker at all and this hung on every click
 *     a first visit          the worker is registered but still installing
 *     workers disabled       a browser setting, or a private window in some
 *                            browsers
 *
 * `currentSubscription` was already fixed for this; the enable path was
 * left on `ready` with the reasoning that "a button somebody pressed has a
 * spinner in front of them". That was exactly backwards — a spinner that
 * never stops is worse than an error, because there is nothing to report
 * and nothing to retry.
 */
async function activeRegistration(): Promise<ServiceWorkerRegistration | null> {
  const existing = await navigator.serviceWorker.getRegistration();
  if (!existing) return null;
  if (existing.active) return existing;

  // Registered and still installing. `ready` is correct *here* — there is a
  // registration, so it will settle — but it is still raced against a
  // timeout, because "will settle" is a claim about a browser rather than a
  // guarantee this code can make.
  return Promise.race([
    navigator.serviceWorker.ready,
    new Promise<null>((resolve) => setTimeout(() => resolve(null), ACTIVATION_TIMEOUT_MS)),
  ]);
}

export type EnableResult =
  | { readonly ok: true; readonly keys: SubscriptionKeys }
  | { readonly ok: false; readonly reason: EnableFailure };

/**
 * The browser half of enabling push — §21, steps 1 to 3.
 *
 * Support, then permission, then subscribe. The **caller** sends the result
 * to the backend and only then turns the preference on, which is §21's
 * ordering and matters: a preference enabled before a subscription exists
 * is a person told push is on with nowhere for it to arrive.
 *
 * Returns a result rather than throwing, because none of these are
 * exceptional — an unsupported browser and a declined prompt are ordinary
 * answers that the UI renders as states (§20).
 *
 * ## Re-subscribing when the key changed
 *
 * An existing subscription is reused, with one exception: if the server's
 * VAPID key is not the one it was created with, the browser will keep
 * returning the old subscription forever and every delivery will be refused
 * by the push service. `pushManager.subscribe` throws in that case, so the
 * old one is discarded and a fresh one requested — the only recovery
 * available, and it is invisible to the person.
 */
export async function enablePush(vapidPublicKey: string): Promise<EnableResult> {
  if (!isPushSupported(pushCapabilities())) {
    return { ok: false, reason: "unsupported" };
  }

  // Explicit, and only ever from a click — see the module docstring.
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    return { ok: false, reason: "denied" };
  }

  const registration = await activeRegistration();
  if (!registration) {
    // No worker, so there is nothing to subscribe through. Reported rather
    // than waited on — see `activeRegistration`.
    return { ok: false, reason: "no-service-worker" };
  }

  try {
    const keys = applicationServerKey(vapidPublicKey);
    const subscription =
      (await registration.pushManager.getSubscription()) ??
      (await registration.pushManager.subscribe({
        // Required by every browser: a subscription that could deliver
        // silently is one a page could use to track somebody, so the
        // platform refuses to create one.
        userVisibleOnly: true,
        applicationServerKey: keys,
      }));

    const serialized = serialize(subscription);
    if (serialized) return { ok: true, keys: serialized };

    // Keys the browser would not expose. Nothing to send, and a stored
    // subscription without them can never be delivered to.
    return { ok: false, reason: "subscribe-failed" };
  } catch {
    // Most often the VAPID key changed under an existing subscription. Drop
    // it and try once more — see the docstring.
    return retryWithFreshSubscription(vapidPublicKey);
  }
}

async function retryWithFreshSubscription(vapidPublicKey: string): Promise<EnableResult> {
  const registration = await activeRegistration();
  if (!registration) return { ok: false, reason: "no-service-worker" };

  try {
    const stale = await registration.pushManager.getSubscription();
    await stale?.unsubscribe();
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: applicationServerKey(vapidPublicKey),
    });
    const serialized = serialize(subscription);
    return serialized
      ? { ok: true, keys: serialized }
      : { ok: false, reason: "subscribe-failed" };
  } catch {
    return { ok: false, reason: "subscribe-failed" };
  }
}

/**
 * Unsubscribes this browser and reports the endpoint that was removed — §22.
 *
 * The endpoint is returned rather than swallowed because the caller needs
 * it to tell the backend which row to revoke, and it is read **before**
 * unsubscribing — a `PushSubscription` whose `unsubscribe()` has resolved
 * still exposes its endpoint, but reading first makes the ordering
 * explicit rather than incidental.
 *
 * Returns `null` when there was nothing to remove, which is not a failure:
 * "this browser is not subscribed" is the state the caller wanted.
 */
export async function disablePush(): Promise<string | null> {
  const subscription = await currentSubscription();
  if (!subscription) return null;
  const { endpoint } = subscription;
  await subscription.unsubscribe();
  return endpoint;
}
