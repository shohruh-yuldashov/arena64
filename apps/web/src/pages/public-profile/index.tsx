import { Link, useParams } from "@tanstack/react-router";

import { isNotFound } from "@/features/profile/model/error-messages";
import { usePlayerRatings, usePublicProfile } from "@/features/profile/model/queries";
import { QueryState } from "@/features/profile/ui/query-state";
import { useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui";
import { ProfileHeader } from "@/widgets/profile-header";
import { RatingCards } from "@/widgets/rating-cards";
import { StatisticsPanel } from "@/widgets/statistics-panel";
import { TournamentHistory } from "@/widgets/tournament-history";

/**
 * Anybody's public profile — `/players/$username`.
 *
 * ## The server filters; this renders what arrived
 *
 * `country`, `is_online` and `last_seen` are **omitted** when the owner has
 * hidden them, and `statistics` is omitted when `show_statistics` is off.
 * So every one of them is rendered conditionally on its presence, and none
 * is defaulted. A client that filled in "Offline" for an absent
 * `is_online` would be publishing a fact the owner chose to withhold.
 *
 * Ratings are the exception and are always shown: they are what pairing is
 * computed from, and privacy settings do not cover them (SPEC-PROFILE).
 *
 * ## No SSR, so the loading and not-found states carry the weight
 *
 * This is a Vite SPA; there is no server render to produce a `404` status.
 * What there is instead is a stable not-found page at the URL that was
 * typed — the address bar keeps it, so a mistyped name is visible.
 *
 * ## Cached apart from the self profile
 *
 * Under `profileKeys.byUsername`, never `me` — see `api/keys.ts` on why
 * sharing a key would let a privacy-filtered page read unfiltered data.
 *
 * No friend or block actions: those are A64-020.4's.
 */
export default function PublicProfilePage() {
  const { t } = useTranslation();
  const { username } = useParams({ from: "/players/$username" });
  const profile = usePublicProfile(username);
  const ratings = usePlayerRatings(profile.data?.id);

  if (profile.isError && isNotFound(profile.error)) {
    return (
      <div className="mx-auto flex max-w-md flex-col items-center gap-4 py-20 text-center">
        <h1 className="text-2xl font-semibold">{t("profile.public.notFoundTitle")}</h1>
        <p className="text-muted-foreground text-sm">{t("profile.public.notFoundBody")}</p>
        <Button asChild className="min-h-11">
          <Link to="/">{t("profile.public.backHome")}</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-8">
      <QueryState
        isPending={profile.isPending}
        isError={profile.isError}
        onRetry={() => void profile.refetch()}
      >
        {profile.data !== undefined && (
          <>
            <ProfileHeader profile={profile.data} />

            {/* Absent means hidden. Rendering a placeholder here would be
                the client inventing a fact from a missing key. */}
            {profile.data.statistics !== null && profile.data.statistics !== undefined ? (
              <StatisticsPanel statistics={profile.data.statistics} />
            ) : (
              <p className="text-muted-foreground text-sm">{t("profile.stats.hidden")}</p>
            )}
          </>
        )}
      </QueryState>

      {ratings.data !== undefined && <RatingCards ratings={ratings.data.ratings} />}

      <TournamentHistory playerId={profile.data?.id} />
    </div>
  );
}
