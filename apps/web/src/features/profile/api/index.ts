import type {
  Avatar,
  MyProfile,
  Preferences,
  PreferencesUpdate,
  PrivacySettings,
  PrivacyUpdate,
  ProfileUpdate,
  PublicProfile,
  TournamentHistoryPage,
} from "@/entities/profile";
import { api } from "@/shared/api";
import { httpClient } from "@/shared/api/client";
import type { components } from "@/shared/api/generated/schema";

/**
 * Every profile call, in one file.
 *
 * Thin by design: a URL, a generated payload type, and nothing else. No
 * error handling — `request` normalises — and no caching, which is
 * `model/`'s. The bearer token is attached by the auth interceptor, so
 * nothing here knows a token exists.
 */
type PlayerRatings = components["schemas"]["PlayerRatingsResponse"];

export function getMyProfile(): Promise<MyProfile> {
  return api.get<MyProfile>("/profile/me");
}

export function getPublicProfile(username: string): Promise<PublicProfile> {
  return api.get<PublicProfile>(`/profiles/${encodeURIComponent(username)}`);
}

export function updateProfile(payload: ProfileUpdate): Promise<MyProfile> {
  return api.patch<MyProfile>("/profile", payload);
}

export function getMyRatings(): Promise<PlayerRatings> {
  return api.get<PlayerRatings>("/ratings/me");
}

export function getPlayerRatings(playerId: string): Promise<PlayerRatings> {
  return api.get<PlayerRatings>(`/players/${playerId}/ratings`);
}

export function getTournamentHistory(
  playerId: string,
  after?: string,
): Promise<TournamentHistoryPage> {
  return api.get<TournamentHistoryPage>(`/players/${playerId}/tournaments`, {
    params: after === undefined ? undefined : { after },
  });
}

export function getPreferences(): Promise<Preferences> {
  return api.get<Preferences>("/profile/preferences");
}

export function updatePreferences(payload: PreferencesUpdate): Promise<Preferences> {
  return api.patch<Preferences>("/profile/preferences", payload);
}

export function getPrivacy(): Promise<PrivacySettings> {
  return api.get<PrivacySettings>("/profile/privacy");
}

export function updatePrivacy(payload: PrivacyUpdate): Promise<PrivacySettings> {
  return api.patch<PrivacySettings>("/profile/privacy", payload);
}

export function getAvatar(): Promise<Avatar> {
  return api.get<Avatar>("/profile/avatar");
}

export function deleteAvatar(): Promise<void> {
  return api.delete<void>("/profile/avatar");
}

/**
 * Uploads an image as `multipart/form-data`.
 *
 * Not through `api.post`, because that sets `Content-Type: application/json`
 * on the shared client — and a multipart body needs the boundary parameter
 * the browser generates, which only appears if the header is left alone.
 * Setting it to `undefined` here is what makes Axios delegate to `FormData`.
 *
 * `signal` is passed through so an upload can be cancelled: a 5 MB image on
 * a slow connection is long enough that navigating away should stop it.
 */
export async function uploadAvatar(file: File, signal?: AbortSignal): Promise<Avatar> {
  const body = new FormData();
  body.append("file", file);

  const response = await httpClient.post<{ data: Avatar }>("/profile/avatar", body, {
    headers: { "Content-Type": undefined },
    ...(signal ? { signal } : {}),
  });
  return response.data.data;
}
