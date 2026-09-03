import type { MyProfile, PublicProfile } from "@/entities/profile";
import { avatarSrc, initialsOf, nameOf } from "@/entities/profile";
import { useTranslation } from "@/shared/i18n";
import { formatDate, formatDateTime } from "@/shared/lib/format";
import { Avatar, AvatarFallback, AvatarImage } from "@/shared/ui";

/**
 * Identity, for both profile surfaces.
 *
 * ## Absent is not the same as false
 *
 * `country`, `is_online` and `last_seen` are **omitted** by the API when
 * privacy hides them. So each is rendered only when present — never as
 * "Offline" or "Country: —", which would be the client inventing a fact
 * from a missing key. The backend decides what a viewer may see; this
 * renders exactly what arrived.
 *
 * That is why the props are optional rather than nullable-with-defaults:
 * the type mirrors the contract, so "hidden" is unrepresentable as "false".
 */
export function ProfileHeader({
  profile,
  avatarVersion,
  children,
}: {
  profile: MyProfile | PublicProfile;
  avatarVersion?: number | null;
  children?: React.ReactNode;
}) {
  const { t, locale } = useTranslation();
  const src = avatarSrc(profile.avatar_url, avatarVersion);
  const country = "country" in profile ? profile.country : null;

  return (
    <header className="flex flex-col gap-4 sm:flex-row sm:items-start">
      <Avatar className="size-20 shrink-0 sm:size-24">
        {src !== null && <AvatarImage src={src} alt="" />}
        <AvatarFallback className="text-xl">{initialsOf(profile)}</AvatarFallback>
      </Avatar>

      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <h1 className="text-2xl font-semibold break-words">{nameOf(profile)}</h1>
        {/* A64-025.9, and the same rule `PlayerRow` follows: only when it
            says something the heading did not. `nameOf` falls back to the
            username, so an account without a display name — which is most
            of them — printed `alice` over `@alice`. */}
        {nameOf(profile) !== profile.username && (
          <p className="text-muted-foreground text-sm">@{profile.username}</p>
        )}

        {profile.bio !== null && profile.bio !== undefined && profile.bio !== "" ? (
          <p className="mt-1 text-sm break-words whitespace-pre-line">{profile.bio}</p>
        ) : null}

        {/* A row of facts rather than a description list: several of these
            are single statements ("Joined 5 August 2026") with no separate
            term, and a `<dl>` with an empty `<dt>` announces worse than a
            plain list does. */}
        <ul className="text-muted-foreground mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          <li>
            {t("profile.header.joined", { date: formatDate(profile.joined_at, locale) ?? "" })}
          </li>

          {/* Each of the three below is rendered only when the API sent it.
              Absent means privacy hid it — see the note above. */}
          {country !== null && country !== undefined && (
            <li>
              {t("profile.header.country")}: {country}
            </li>
          )}

          {profile.is_online !== null && profile.is_online !== undefined && (
            <li className="flex items-center gap-1">
              <span
                aria-hidden="true"
                className={
                  profile.is_online
                    ? "size-2 rounded-full bg-success"
                    : "bg-muted-foreground/50 size-2 rounded-full"
                }
              />
              {/* The word, not only the dot — colour is never the sole
                  indicator (WCAG 1.4.1). */}
              {profile.is_online ? t("profile.header.online") : t("profile.header.offline")}
            </li>
          )}

          {profile.last_seen !== null && profile.last_seen !== undefined && (
            <li>
              {t("profile.header.lastSeen", {
                when: formatDateTime(profile.last_seen, locale) ?? "",
              })}
            </li>
          )}
        </ul>

        {children}
      </div>
    </header>
  );
}
