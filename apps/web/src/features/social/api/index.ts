import { api } from "@/shared/api";
import type { components } from "@/shared/api/generated/schema";

/**
 * Every social call, in one file.
 *
 * A URL, a generated payload type, nothing else. **Every target is a
 * `player_id` the server previously returned** — never a username typed by
 * the user and never an id from a form. The sender is always the session,
 * which the API takes from the access token and no parameter could name.
 */
type Schemas = components["schemas"];

export type FriendPage = Schemas["CursorPage_FriendResponse_"];
export type RequestPage = Schemas["CursorPage_FriendRequestResponse_"];
export type BlockedPage = Schemas["CursorPage_BlockedPlayerResponse_"];
export type SearchPage = Schemas["CursorPage_ProfileResponse_"];
export type FriendRequest = Schemas["FriendRequestResponse"];

/** The API's own floor — `q` is 2–50 characters. Below it, no request. */
export const MIN_QUERY_LENGTH = 2;

/**
 * `signal` is threaded through so a superseded search is genuinely
 * cancelled rather than merely ignored — on a slow connection the obsolete
 * request is still in flight when the next keystroke lands.
 */
export function searchPlayers(
  query: string,
  cursor?: string,
  signal?: AbortSignal,
): Promise<SearchPage> {
  return api.get<SearchPage>("/users/search", {
    params: { q: query, ...(cursor === undefined ? {} : { cursor }) },
    ...(signal ? { signal } : {}),
  });
}

export function listFriends(cursor?: string): Promise<FriendPage> {
  return api.get<FriendPage>("/friends", {
    params: cursor === undefined ? undefined : { cursor },
  });
}

export function countFriends(): Promise<Schemas["FriendCountResponse"]> {
  return api.get<Schemas["FriendCountResponse"]>("/friends/count");
}

export function removeFriend(playerId: string): Promise<void> {
  return api.delete<void>(`/friends/${playerId}`);
}

export function listIncoming(cursor?: string): Promise<RequestPage> {
  return api.get<RequestPage>("/friends/requests/incoming", {
    params: cursor === undefined ? undefined : { cursor },
  });
}

export function listOutgoing(cursor?: string): Promise<RequestPage> {
  return api.get<RequestPage>("/friends/requests/outgoing", {
    params: cursor === undefined ? undefined : { cursor },
  });
}

export function sendRequest(playerId: string): Promise<FriendRequest> {
  return api.post<FriendRequest>("/friends/requests", { player_id: playerId });
}

export function acceptRequest(requestId: string): Promise<FriendRequest> {
  return api.post<FriendRequest>(`/friends/requests/${requestId}/accept`);
}

export function declineRequest(requestId: string): Promise<FriendRequest> {
  return api.post<FriendRequest>(`/friends/requests/${requestId}/decline`);
}

/** Withdraws a request **the viewer sent**. Distinct from declining one. */
export function cancelRequest(requestId: string): Promise<void> {
  return api.delete<void>(`/friends/requests/${requestId}`);
}

export function listBlocked(cursor?: string): Promise<BlockedPage> {
  return api.get<BlockedPage>("/blocks", {
    params: cursor === undefined ? undefined : { cursor },
  });
}

export function blockPlayer(playerId: string): Promise<unknown> {
  return api.post("/blocks", { player_id: playerId });
}

export function unblockPlayer(playerId: string): Promise<void> {
  return api.delete<void>(`/blocks/${playerId}`);
}
