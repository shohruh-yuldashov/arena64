import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * The three push subscription calls — A64-021.6 §4.
 *
 * Scoped to the signed-in player by the **access token**, not by a
 * parameter: there is no user id to send, so this client cannot subscribe
 * somebody else's account even by accident. The backend forbids the field
 * outright, so an attempt is a `422` rather than a silent no-op.
 *
 * What travels is what the *browser* produced — an endpoint and two keys —
 * and nothing else. No payload, no target URL, no VAPID key.
 */
type Schemas = components["schemas"];

export type PushStatus = Schemas["PushStatusResponse"];
export type PushSubscriptionRegistration = Schemas["PushSubscriptionResponse"];

export function readPushStatus(): Promise<PushStatus> {
  return api.get<PushStatus>("/notifications/push/status");
}

export function registerPushSubscription(
  body: Schemas["RegisterPushSubscriptionRequest"],
): Promise<PushSubscriptionRegistration> {
  return api.post<PushSubscriptionRegistration>("/notifications/push/subscriptions", body);
}

/**
 * Removes this browser's subscription.
 *
 * `POST .../remove` rather than `DELETE`, and the backend's route docstring
 * carries the reason: the endpoint is a bearer capability, and both `DELETE`
 * shapes put it somewhere worse — in a URL that lands in access logs, or in
 * a body that intermediaries strip.
 */
export function removePushSubscription(endpoint: string): Promise<void> {
  return api.post<void>("/notifications/push/subscriptions/remove", { endpoint });
}
