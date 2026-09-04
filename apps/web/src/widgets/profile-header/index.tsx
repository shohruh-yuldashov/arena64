import type { MyProfile, PlayerRating, PublicProfile } from "@/entities/profile";
import { avatarSrc, initialsOf, nameOf, primaryRating } from "@/entities/profile";
import { speedClassKey } from "@/entities/time-control";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatDate, formatDateTime, formatNumber } from "@/shared/lib/format";
import { speedAccent } from "@/shared/lib/speed-accent";
import { Avatar, AvatarFallback, AvatarImage } from "@/shared/ui";

/**
 * Identity, for both profile surfaces.
 *
 * ## What the band leads with — A64-025.9
 *
 * Who this is, and how strong they are. Those are the two questions a
 * profile is opened to answer, so the name and the player's leading
 * standing are the two largest things on the page and sit on one line
 * together. Everything the header used to open with — the join date, the
 * country, the last-seen time — is true but is nobody's reason for
 * arriving, so it reads as a quiet meta row underneath.
 *
 * The standing shown is `primaryRating`'s: the category actually played
 * most, never the highest number. See that function for why a page that
 * led with the maximum would be advertising a category nobody has entered.
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
 *
 * `ratings` is optional for the same kind of reason, but a different one:
 * both pages load them in a **second** request, so the band renders without
 * them and gains the standing when it arrives rather than holding the whole
 * identity back for a number.
 */
export function ProfileHeader({
  profile,
  ratings,
  avatarVersion,
  children,
}: {
  profile: MyProfile | PublicProfile;
  ratings?: PlayerRating[];
  avatarVersion?: number | null;
  children?: React.ReactNode;
}) {
  const { t, locale } = useTranslation();
  const src = avatarSrc(profile.avatar_url, avatarVersion);
  const country = "country" in profile ? profile.country : null;
  const standing = ratings === undefined ? null : primaryRating(ratings);
  const bio = profile.bio?.trim();

  return (
    <header className="border-border bg-card flex flex-col gap-5 rounded-xl border p-5 sm:p-7">
      <div className="flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between sm:gap-8">
        <div className="flex min-w-0 items-center gap-4 sm:gap-5">
          <Avatar className="size-16 shrink-0 sm:size-20">
            {src !== null && <AvatarImage src={src} alt="" />}
            <AvatarFallback className="text-xl">{initialsOf(profile)}</AvatarFallback>
          </Avatar>

          <div className="flex min-w-0 flex-col gap-1">
            <h1 className="text-2xl font-semibold tracking-tight break-words sm:text-3xl">
              {nameOf(profile)}
            </h1>
            {/* A64-025.9, and the same rule `PlayerRow` follows: only when it
                says something the heading did not. `nameOf` falls back to the
                username, so an account without a display name — which is most
                of them — printed `alice` over `@alice`. */}
            {nameOf(profile) !== profile.username && (
              <p className="text-muted-foreground text-sm">@{profile.username}</p>
            )}

            {/* Presence sits with the identity rather than in the meta row:
                whether somebody is here right now is the one fact a visitor
                acts on, and it was previously the fifth item in a list of
                dates. Rendered only when the API sent it — absent means
                privacy hid it, see the note above. */}
            {profile.is_online !== null && profile.is_online !== undefined && (
              <span className="mt-0.5 inline-flex w-fit items-center gap-1.5 text-xs font-medium">
                <span
                  aria-hidden="true"
                  className={
                    profile.is_online
                      ? "bg-success size-2 rounded-full"
                      : "bg-muted-foreground/40 size-2 rounded-full"
                  }
                />
                {/* The word, not only the dot — colour is never the sole
                    indicator (WCAG 1.4.1). */}
                <span className={profile.is_online ? "" : "text-muted-foreground"}>
                  {profile.is_online ? t("profile.header.online") : t("profile.header.offline")}
                </span>
              </span>
            )}
          </div>
        </div>

        {standing !== null && (
          // The class's own hue, not the brand's — §18.7. This figure *is* a
          // rating in that class, and the card for it further down the page
          // carries the same colour; a brand-purple panel above an orange
          // Blitz card would be one fact wearing two colours.
          <div
            className={cn(
              "flex shrink-0 flex-col gap-0.5 rounded-xl border p-4 sm:min-w-52 sm:p-5",
              speedAccent(standing.speed_class).panel,
            )}
          >
            <span
              className={cn("text-xs font-semibold", speedAccent(standing.speed_class).text)}
            >
              {t("profile.ratings.inCategory", {
                category: t(speedClassKey(standing.speed_class)),
              })}
            </span>
            <span className="text-4xl leading-none font-semibold tracking-tight tabular-nums">
              {formatNumber(Math.round(standing.rating), locale)}
            </span>
            <span className="text-muted-foreground text-xs">
              {t("profile.ratings.games", { count: standing.games_played })}
              {standing.is_provisional && ` · ${t("profile.ratings.provisional")}`}
            </span>
          </div>
        )}
      </div>

      {bio !== undefined && bio !== "" ? (
        <p className="max-w-prose text-sm break-words whitespace-pre-line">{bio}</p>
      ) : null}

      {/* A row of facts rather than a description list: several of these
          are single statements ("Joined 5 August 2026") with no separate
          term, and a `<dl>` with an empty `<dt>` announces worse than a
          plain list does. */}
      <ul className="text-muted-foreground flex flex-wrap gap-x-4 gap-y-1 text-xs">
        <li>
          {t("profile.header.joined", { date: formatDate(profile.joined_at, locale) ?? "" })}
        </li>

        {country !== null && country !== undefined && (
          <li>
            {t("profile.header.country")}: {country}
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
    </header>
  );
}
