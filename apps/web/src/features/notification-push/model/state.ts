import type { PushStatus } from "@/features/notification-push/api";
import { isPushSupported, pushCapabilities } from "@/shared/pwa";

import { permissionState } from "./subscription";

/**
 * The eight states §20 forbids compressing into a boolean.
 *
 * ## Why this is a function and not a component's `useMemo`
 *
 * §20 lists eight distinguishable situations and says not to flatten them.
 * The flattening happens by accident — a component that renders
 * `disabled={!available || permission !== "granted"}` has *already* lost
 * the difference between "your browser cannot do this" and "you said no",
 * and those need opposite instructions: one is unfixable, the other is
 * fixable only in browser settings.
 *
 * So the resolution is one pure function over three inputs, and the
 * component renders a message per state. A state added later fails the
 * exhaustive switch rather than falling into a plausible default.
 *
 * ## The three inputs, and why none of them is enough alone
 *
 *   the **server** knows whether it holds a VAPID pair and how many
 *     browsers this account has registered. It cannot see a permission
 *     prompt;
 *   the **browser** knows whether it supports push and what the person
 *     answered. It does not know whether the server can send;
 *   this **browser's** subscription says whether *this* device is one of
 *     the registered ones — a person with push on their phone and not their
 *     laptop is in two different states on two devices.
 */

export type PushState =
  /** No `PushManager`, no service worker, or no `Notification`. */
  | "unsupported"
  /** The server holds no VAPID key pair. Nothing to enable. */
  | "unavailable"
  /** Supported and available; the person has not been asked yet. */
  | "askable"
  /** They said no. Only browser settings can undo this. */
  | "denied"
  /** Permission granted, but this browser is not subscribed. */
  | "not-subscribed"
  /** This browser is subscribed and push is on. */
  | "active"
  /** This browser is subscribed and the person muted the channel. */
  | "muted";

export interface PushInputs {
  /** `GET /notifications/push/status`, or `undefined` while it loads. */
  readonly status: PushStatus | undefined;
  /** Whether this browser holds a live `PushSubscription`. */
  readonly subscribed: boolean;
  /** Whether any push preference is on — the matrix's own answer. */
  readonly preferenceEnabled: boolean;
}

/**
 * Resolves the state, most-final first.
 *
 * The ordering is the point. An unsupported browser is unsupported whatever
 * the server says, and a server that cannot send makes the permission
 * irrelevant — so the two facts nothing can change come first, and the ones
 * a person can act on come after.
 */
export function pushStateOf({
  status,
  subscribed,
  preferenceEnabled,
}: PushInputs): PushState | "loading" {
  if (!isPushSupported(pushCapabilities())) return "unsupported";
  if (status === undefined) return "loading";
  if (!status.available) return "unavailable";

  const permission = permissionState();
  if (permission === "denied") return "denied";
  if (permission === "default") return "askable";

  if (!subscribed) return "not-subscribed";
  return preferenceEnabled ? "active" : "muted";
}

/** Whether the enable action should be offered at all. */
export function canEnable(state: PushState | "loading"): boolean {
  return state === "askable" || state === "not-subscribed" || state === "muted";
}
