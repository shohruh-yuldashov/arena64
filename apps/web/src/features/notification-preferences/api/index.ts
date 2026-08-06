import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * The two preference calls — A64-021.3 §14.
 *
 * Scoped to the signed-in player by the **access token**, not by a
 * parameter: there is no user id to send, so this client cannot ask for
 * somebody else's settings even by accident.
 *
 * Both calls return the **whole matrix**, which is why the update needs no
 * follow-up read: what a `PATCH` answers is exactly what a fresh `GET`
 * would say.
 */
type Schemas = components["schemas"];

export type NotificationPreferences = Schemas["NotificationPreferencesResponse"];
export type PreferenceSetting = Schemas["PreferenceSettingResponse"];
export type PreferenceChange = Schemas["PreferenceChangeRequest"];
export type NotificationCategory = Schemas["NotificationCategory"];
export type DeliveryChannel = Schemas["DeliveryChannel"];

export function readNotificationPreferences(): Promise<NotificationPreferences> {
  return api.get<NotificationPreferences>("/notifications/preferences");
}

/**
 * Sends **only** the switches that moved.
 *
 * Not the whole grid: a client that sent every cell would overwrite a
 * category it never rendered the day the backend adds one, and would
 * silently revert a change a second tab made between the read and the save.
 */
export function updateNotificationPreferences(
  changes: PreferenceChange[],
): Promise<NotificationPreferences> {
  return api.patch<NotificationPreferences>("/notifications/preferences", { changes });
}
