import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { PreferencesUpdate, PrivacyUpdate, ProfileUpdate } from "@/entities/profile";
import * as profileApi from "@/features/profile/api";
import { profileKeys } from "@/features/profile/api/keys";

/**
 * The read and write hooks — A64-020.3 §15.
 *
 * ## What each mutation invalidates, and why not more
 *
 * Blanket `invalidateQueries()` after every write is the easy answer and
 * the wrong one: it refetches the tournament history because somebody
 * changed their bio. Each mutation below names exactly the keys its write
 * can have changed.
 *
 *   profile edit  → `me`, and every public profile (the display name and
 *                   bio appear on both surfaces)
 *   avatar        → the same, for the same reason
 *   privacy       → **public profiles only**. Privacy filters what others
 *                   see; `/profile/me` is unfiltered and cannot change
 *   preferences   → its own key. Nothing else reads it
 *
 * Public profiles are invalidated by **prefix** (`publicAll`) rather than
 * by username, because a client does not reliably know its own username
 * key — `/players/{someone-else}` is cached under theirs, and after a
 * privacy change every cached public profile is potentially stale anyway.
 */

/** The signed-in account. `/profile/me` carries no ratings — see below. */
export function useMyProfile() {
  return useQuery({
    queryKey: profileKeys.me(),
    queryFn: profileApi.getMyProfile,
  });
}

/**
 * The caller's ratings, as a **second** query rather than a field.
 *
 * `MyProfileResponse` does not carry ratings; `ProfileResponse` does. That
 * asymmetry is the API's, and the honest way to live with it is one extra
 * request rather than a fabricated shape. Two fixed queries is not an N+1 —
 * the count does not grow with anything.
 */
export function useMyRatings() {
  return useQuery({
    queryKey: profileKeys.myRatings(),
    queryFn: profileApi.getMyRatings,
  });
}

/**
 * Anybody's public profile.
 *
 * `enabled` is **not optional**, and the reason is a defect the social e2e
 * caught: this query fires on mount, the session bootstraps in an effect,
 * and on a direct navigation the fetch wins the race. The response is then
 * composed for an *anonymous* viewer — `relationship` is `null` — and that
 * answer is cached, so a signed-in player sees a profile with no social
 * actions until something evicts it.
 *
 * So the caller passes `isResolved(session)` and the query waits. The cost
 * is one render of the pending state on a cold load; the alternative is a
 * page that silently disagrees with the session about who is looking.
 */
export function usePublicProfile(username: string, enabled: boolean) {
  return useQuery({
    queryKey: profileKeys.byUsername(username),
    queryFn: () => profileApi.getPublicProfile(username),
    enabled,
    // A wrong username is a `404` and will stay one. Retrying it three
    // times delays the not-found page for no possible gain.
    retry: false,
  });
}

export function usePlayerRatings(playerId: string | undefined) {
  return useQuery({
    queryKey: profileKeys.ratings(playerId ?? ""),
    queryFn: () => profileApi.getPlayerRatings(playerId as string),
    enabled: playerId !== undefined,
  });
}

/**
 * A player's tournaments, paged by the API's opaque cursor.
 *
 * `useInfiniteQuery` because the endpoint is keyset — there is no page
 * number to jump to, only "after this row". `getNextPageParam` returns the
 * cursor the server issued, unread, which is the contract
 * (SPEC-TOURNAMENT §6g).
 *
 * **One request per page, never per row.** The endpoint returns each
 * tournament's summary in the same statement (A64-020.0C), so a row needs
 * no follow-up call — and a component that fetched a detail per row would
 * reintroduce the N+1 that phase removed on the server.
 */
export function useTournamentHistory(playerId: string | undefined) {
  return useInfiniteQuery({
    queryKey: profileKeys.tournaments(playerId ?? ""),
    queryFn: ({ pageParam }) =>
      profileApi.getTournamentHistory(playerId as string, pageParam ?? undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: playerId !== undefined,
  });
}

export function usePreferences() {
  return useQuery({
    queryKey: profileKeys.preferences(),
    queryFn: profileApi.getPreferences,
  });
}

export function usePrivacy() {
  return useQuery({
    queryKey: profileKeys.privacy(),
    queryFn: profileApi.getPrivacy,
  });
}

export function useUpdateProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: ProfileUpdate) => profileApi.updateProfile(payload),
    // **Not optimistic.** The server normalises what it stores — trimming,
    // and rejecting a display name the client thought was fine — so an
    // optimistic write would show a value that is about to change. The
    // response *is* the new profile, so it is written straight into the
    // cache and the refetch below is only for the public surfaces.
    onSuccess: (updated) => {
      queryClient.setQueryData(profileKeys.me(), updated);
      void queryClient.invalidateQueries({ queryKey: profileKeys.publicAll() });
    },
  });
}

export function useUpdatePreferences() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: PreferencesUpdate) => profileApi.updatePreferences(payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(profileKeys.preferences(), updated);
    },
  });
}

export function useUpdatePrivacy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: PrivacyUpdate) => profileApi.updatePrivacy(payload),
    onSuccess: (updated) => {
      queryClient.setQueryData(profileKeys.privacy(), updated);
      // The public surfaces are what privacy changes, and **only** those.
      // The server decides what a viewer may see; the client never predicts
      // the filtering, it refetches and renders whatever comes back.
      void queryClient.invalidateQueries({ queryKey: profileKeys.publicAll() });
    },
  });
}

export function useUploadAvatar() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, signal }: { file: File; signal?: AbortSignal }) =>
      profileApi.uploadAvatar(file, signal),
    onSuccess: () => {
      // Not `setQueryData`: the avatar response carries the image, not the
      // profile, and `/profile/me` is what holds `avatar_url`. Refetching
      // is one request and cannot leave the two disagreeing.
      void queryClient.invalidateQueries({ queryKey: profileKeys.me() });
      void queryClient.invalidateQueries({ queryKey: profileKeys.publicAll() });
    },
  });
}

export function useDeleteAvatar() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: profileApi.deleteAvatar,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: profileKeys.me() });
      void queryClient.invalidateQueries({ queryKey: profileKeys.publicAll() });
    },
  });
}
