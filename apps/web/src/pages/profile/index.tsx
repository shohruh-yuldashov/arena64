import { Link } from "@tanstack/react-router";

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
 * The identity band is the one exception in shape rather than in timing: it
 * renders from `/profile/me` alone and gains the leading standing when
 * `/ratings/me` lands, because a name that waits for a number shows nothing
 * for the length of the slower request.
 *
 * ## Viewing, not editing — A64-025.9
 *
 * The avatar used to be uploadable from here, which drew the same picture
 * twice on one screen — once as identity, once as a file field — while
 * every other editable thing lived behind "Edit profile". `AvatarManager`
 * now sits on `/settings/profile` with the rest of them, and this page
 * shows the profile rather than being a second, partial editor for it.
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
            <ProfileHeader profile={profile.data} ratings={ratings.data?.ratings}>
              {/* Stacked below `sm`, a row above — A64-025.5D.
                  "Match history" was a third action here until A64-025.5B
                  and is a **section** of the product: it sits in the header
                  at every width, and offering it a second time on one
                  profile out of many is a navigation model that disagrees
                  with itself.
                  
                  Two columns at 360 was the arrangement that replaced it,
                  and it clipped: "Ommaviy profilni ko'rish" and "Открыть
                  публичный профиль" do not fit in half of 360, and English
                  never showed it. Full width each, which cannot clip in any
                  language. */}
              <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
                <Button asChild variant="outline" className="min-h-11 w-full sm:w-auto">
                  <Link to="/settings/profile">{t("profile.nav.editProfile")}</Link>
                </Button>
                <Button asChild variant="ghost" className="min-h-11 w-full sm:w-auto">
                  <Link to="/players/$username" params={{ username: profile.data.username }}>
                    {t("profile.header.viewPublic")}
                  </Link>
                </Button>
              </div>
            </ProfileHeader>

            <StatisticsPanel statistics={profile.data.statistics} />
          </>
        )}
      </QueryState>

      {ratings.data !== undefined && <RatingCards ratings={ratings.data.ratings} />}

      <TournamentHistory playerId={profile.data?.id} />
    </div>
  );
}
