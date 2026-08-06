import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * The four notification calls — A64-021.1 §15.
 *
 * Every one of them is scoped to the signed-in player by the **access
 * token**, not by a parameter: there is no recipient id to send, which is
 * what makes it impossible for this client to ask for somebody else's
 * notifications even by accident.
 *
 * One request per page, and none per row: the actor's name and avatar
 * arrive composed in the response (§31), so nothing here fetches a profile.
 */
type Schemas = components["schemas"];

export type NotificationPage = Schemas["NotificationPageResponse"];
export type Notification = Schemas["NotificationResponse"];
export type NotificationActor = Schemas["NotificationActorResponse"];
export type NotificationTarget = Schemas["NotificationTargetResponse"];
export type UnreadCount = Schemas["UnreadCountResponse"];
export type MarkReadResult = Schemas["MarkAllReadResponse"];

export function readNotifications(
  options: { after?: string | null; limit?: number } = {},
): Promise<NotificationPage> {
  const query = new URLSearchParams();
  // **The cursor is opaque and is sent back verbatim** — §19. Decoding it
  // would couple this client to a base64 encoding of two server-side
  // columns, which is exactly what an opaque cursor exists to prevent.
  if (options.after) query.set("after", options.after);
  if (options.limit) query.set("limit", String(options.limit));

  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return api.get<NotificationPage>(`/notifications${suffix}`);
}

export function readUnreadCount(): Promise<UnreadCount> {
  return api.get<UnreadCount>("/notifications/unread-count");
}

export function markNotificationRead(notificationId: string): Promise<MarkReadResult> {
  return api.post<MarkReadResult>(`/notifications/${notificationId}/read`);
}

export function markAllNotificationsRead(): Promise<MarkReadResult> {
  return api.post<MarkReadResult>("/notifications/read-all");
}
