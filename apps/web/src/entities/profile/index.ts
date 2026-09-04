import type { components } from "@/shared/api/generated/schema";

/**
 * A player, as the API describes them.
 *
 * Aliases over the **generated** schema, never re-declarations. The two
 * profile shapes are genuinely different types and are kept apart on
 * purpose:
 *
 *   `MyProfile`     what `/profile/me` returns — the signed-in account,
 *                   unfiltered, and **without ratings**
 *   `PublicProfile` what `/profiles/{username}` returns — privacy-filtered,
 *                   and **with** ratings
 *
 * Collapsing them into one optional-everything type would make every
 * consumer guard fields that are guaranteed on one surface, and would let a
 * public component read a field only the self surface has. They are cached
 * under separate query keys for the same reason.
 */
type Schemas = components["schemas"];

export type MyProfile = Schemas["MyProfileResponse"];
export type PublicProfile = Schemas["ProfileResponse"];
export type ProfileStatistics = Schemas["StatisticsResponse"];
export type ProfileUpdate = Schemas["ProfileUpdateRequest"];
export type Preferences = Schemas["PreferencesResponse"];
export type PreferencesUpdate = Schemas["PreferencesUpdateRequest"];
export type PrivacySettings = Schemas["PrivacySettingsResponse"];
export type PrivacyUpdate = Schemas["PrivacySettingsUpdateRequest"];
export type Avatar = Schemas["AvatarResponse"];
export type PlayerRatings = Schemas["PlayerRatingsResponse"];
/**
 * One `(variant, speed_class)` standing — `rating.presentation`'s shape,
 * not `profiles`'. Two schemas share the name `RatingResponse` on the API,
 * so the generator disambiguates by module path; the fully-qualified name
 * is ugly and is the correct one to depend on.
 */
export type PlayerRating =
  Schemas["app__modules__rating__presentation__schemas__ratings__RatingResponse"];

/** The three categories a **public** profile carries — `profiles`' own. */
export type ProfileRatings = Schemas["RatingsResponse"];
export type ProfileRating =
  Schemas["app__modules__profiles__presentation__schemas__profile__RatingResponse"];
export type TournamentHistoryPage = Schemas["PlayerTournamentsResponse"];

/**
 * The audience a privacy setting may name.
 *
 * `show_country`, `show_last_seen`, `show_online_status` and `show_activity`
 * are **deprecated** on the API — each is `true` only when its richer
 * counterpart is `everyone`, so reading them loses the friends-only case.
 * The UI reads `last_seen`, `online_status` and `activity`.
 */
export type PrivacyAudience = PrivacySettings["last_seen"];

/** Initials for an avatar with no image. Never an empty string. */
export function initialsOf(profile: {
  display_name?: string | null;
  username: string;
}): string {
  const source = profile.display_name?.trim() || profile.username;
  return source.slice(0, 2).toUpperCase();
}

/** The name to render. `display_name` is nullable on both surfaces. */
export function nameOf(profile: { display_name?: string | null; username: string }): string {
  return profile.display_name?.trim() || profile.username;
}

/**
 * The avatar URL, versioned so a replacement is actually seen.
 *
 * The storage layer serves the same URL for a replaced image, so a browser
 * that cached the old one keeps showing it — a user uploads a new picture
 * and nothing changes, which reads as a broken upload. `avatar_version`
 * comes from the API and changes on every write; appending it makes the URL
 * different without the client knowing anything about object keys.
 *
 * `null` when there is no avatar, so a caller renders the fallback rather
 * than requesting a URL that 404s.
 */
export function avatarSrc(
  url: string | null | undefined,
  version?: number | null,
): string | null {
  if (url === null || url === undefined || url === "") return null;
  if (version === null || version === undefined) return url;
  return `${url}${url.includes("?") ? "&" : "?"}v=${version}`;
}

/**
 * Wins as a share of decided games, or `null` when there are none.
 *
 * **Presentation only.** `StatisticsResponse.win_rate` is the backend's own
 * figure and is what gets rendered; this exists for the one case the API
 * cannot express — nothing has been played, where the honest answer is "no
 * games yet" rather than `0%`, which reads as "lost everything".
 */
export function winRateLabel(statistics: ProfileStatistics): number | null {
  return statistics.games_played === 0 ? null : statistics.win_rate;
}

/**
 * The standing a profile leads with, or `null` when nothing has been played.
 *
 * **The one with the most games**, and ties broken by the higher rating.
 * `/ratings/me` returns every speed class, marking the unplayed ones
 * provisional at the starting value — so "highest rating" would crown a
 * category nobody has entered, and "first in the list" would crown whatever
 * order the API happened to send.
 *
 * Presentation only, and deliberately so: this decides which number a page
 * shows *largest*, not which one is authoritative. Every rating remains
 * exactly what the server sent.
 */
export function primaryRating(ratings: PlayerRating[]): PlayerRating | null {
  const played = ratings.filter((rating) => rating.games_played > 0);
  if (played.length === 0) return null;

  return played.reduce((best, candidate) =>
    candidate.games_played !== best.games_played
      ? candidate.games_played > best.games_played
        ? candidate
        : best
      : candidate.rating > best.rating
        ? candidate
        : best,
  );
}
