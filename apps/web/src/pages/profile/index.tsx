import { Link } from "@tanstack/react-router";

import { AvatarManager } from "@/features/avatar";
import { useMyProfile, useMyRatings } from "@/features/profile/model/queries";
import { QueryState } from "@/features/profile/ui/query-state";
import { useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui";
import { ProfileHeader } from "@/widgets/profile-header";
import { RatingCards } from "@/widgets/rating-cards";
import { StatisticsPanel } from "@/widgets/statistics-panel";
import { TournamentHistory } from "@/widgets/tournament-history";

/**
 * The signed-in player's own profile.
 *
 * ## Three queries, and why that is not an N+1
 *
 *     /profile/me                    identity, bio, statistics
 *     /ratings/me                    every rating category
 *     /players/{id}/tournaments      history, one page at a time
 *
 * `MyProfileResponse` carries statistics but **not** ratings — that
 * asymmetry is the API's, and the honest response is one extra request
 * rather than a fabricated shape. The count is fixed: it does not grow with
 * the number of ratings, tournaments or anything else on the page, which is
 * what an N+1 would mean.
 *
 * Each section renders as its own data arrives. A page that waited for the
 * slowest of three would show nothing for as long as the worst one takes.
 */
export default function ProfilePage() {
  const { t } = useTranslation();
  const profile = useMyProfile();
  const ratings = useMyRatings();

  return (
    <div className="flex flex-col gap-8">
      <QueryState
        isPending={profile.isPending}
        isError={profile.isError}
        onRetry={() => void profile.refetch()}
      >
        {profile.data !== undefined && (
          <>
            <ProfileHeader profile={profile.data}>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button asChild variant="outline" className="min-h-11">
                  <Link to="/settings/profile">{t("profile.nav.editProfile")}</Link>
                </Button>
                <Button asChild variant="ghost" className="min-h-11">
                  <Link to="/players/$username" params={{ username: profile.data.username }}>
                    {t("profile.header.viewPublic")}
                  </Link>
                </Button>
                {/* A64-020.5F §20. A link, not an inline preview: the
                    profile's request count stays where it was, and the
                    history page owns its own pagination. */}
                <Button asChild variant="ghost" className="min-h-11">
                  <Link to="/games/history">{t("history.title")}</Link>
                </Button>
              </div>
            </ProfileHeader>

            <AvatarManager profile={profile.data} />
            <StatisticsPanel statistics={profile.data.statistics} />
          </>
        )}
      </QueryState>

      {ratings.data !== undefined && <RatingCards ratings={ratings.data.ratings} />}

      <TournamentHistory playerId={profile.data?.id} />
    </div>
  );
}
