import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * The two device calls — A64-030.5C, SE-2.
 *
 * ## Why `/auth/browser` and not `/auth`
 *
 * The list marks which row is *this* device, and the only credential that
 * can say so is the refresh cookie — an access token names an account, not
 * a device. That cookie's path is `/api/v1/auth/browser`, so a request to
 * any other prefix simply does not carry it and every row would come back
 * unmarked. The backend's `browser_router.py` records the alternatives that
 * were rejected.
 *
 * Scoped to the signed-in player by the **access token**, not a parameter:
 * there is no user id to send, so this client cannot read or revoke
 * somebody else's devices even by accident.
 */
type Schemas = components["schemas"];

export type SessionRead = Schemas["SessionRead"];

const BROWSER = "/auth/browser";

export function readSessions(): Promise<SessionRead[]> {
  return api.get<SessionRead[]>(`${BROWSER}/sessions`);
}

/**
 * Signs one device out.
 *
 * The id is a **device**, not one rotation of its credential, which is why
 * an id read a while ago still works: the browser rotates its refresh token
 * roughly every fifteen minutes and the identity this names survives that.
 *
 * `204` on success and on a device that was already signed out — a retry
 * after a dropped response is not an error. A `404` means the id is not one
 * of this account's devices.
 */
export function revokeSession(id: string): Promise<void> {
  return api.delete<void>(`${BROWSER}/sessions/${id}`);
}
