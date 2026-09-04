import { Link, useParams } from "@tanstack/react-router";

import { isResolved } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { ChallengeButton } from "@/features/challenges/ui/challenge-button";
import { isNotFound } from "@/features/profile/model/error-messages";
import { usePlayerRatings, usePublicProfile } from "@/features/profile/model/queries";
import { QueryState } from "@/features/profile/ui/query-state";
import { RelationshipActions } from "@/features/social/ui/relationship-actions";
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
 * ## Social actions — A64-020.4
 *
 * Rendered through `ProfileHeader`'s `children` seam, from the
 * `relationship` field the **same response** carries. No second request:
 * the composer resolves it in the batch that built this profile, so a
 * relationship-aware page costs exactly what an unaware one did.
 *
 * Nothing is rendered on the viewer's own profile, and nothing for an
 * anonymous reader — the API sends `null` for both, and `actionsFor(null)`
 * is an empty list. That is one rule in one place rather than a check here
 * and another in every list.
 *
 * `requestId` is deliberately not passed: a profile response names a
 * relationship, not the request row that produced it, so accept and decline
 * are offered on `/friends/requests` where the id exists. Guessing one
 * would mean a lookup this page does not need.
 */
export default function PublicProfilePage() {
  const { t } = useTranslation();
  const { username } = useParams({ from: "/players/$username" });
  const { state: session } = useSession();
  // Waits for the session, so the profile is never composed for an
  // anonymous viewer and cached that way — see `usePublicProfile`.
  const profile = usePublicProfile(username, isResolved(session));
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
        isPending={profile.isPending || !isResolved(session)}
        isError={profile.isError}
        onRetry={() => void profile.refetch()}
      >
        {profile.data !== undefined && (
          <>
            <ProfileHeader profile={profile.data} ratings={ratings.data?.ratings}>
              {/* Belt and braces: the API already omits `relationship` on
                  the viewer's own profile, and this makes the "never on
                  your own page" rule visible where somebody reading the
                  page would look for it. */}
              {session.status === "authenticated" && session.user.id !== profile.data.id && (
                <div className="flex flex-wrap items-center gap-2">
                  {/* A64-022.5 §13. Rendered from the **same** `relationship`
                      the actions beside it read — one server-computed value,
                      so the button cannot appear for a stranger, a blocked
                      player, or the viewer's own page. */}
                  <ChallengeButton
                    playerId={profile.data.id}
                    playerName={profile.data.display_name ?? profile.data.username}
                    state={profile.data.relationship}
                    size="default"
                  />
                  <RelationshipActions
                    playerId={profile.data.id}
                    playerName={profile.data.display_name ?? profile.data.username}
                    state={profile.data.relationship}
                    size="default"
                    // One player on their own page, so the destructive
                    // actions keep §18.8's red text — A64-025.8B §27.
                    tone="detail"
                  />
                </div>
              )}
            </ProfileHeader>

            {/* Absent means hidden. Rendering a placeholder here would be
                the client inventing a fact from a missing key. */}
            {profile.data.statistics !== null && profile.data.statistics !== undefined ? (
              <StatisticsPanel statistics={profile.data.statistics} />
            ) : (
              // The same dashed frame the unrated categories and an empty
              // tournament history use — A64-025.9 §18.8. It was a bare
              // paragraph between two carded sections, which read as text
              // that had lost its container rather than as a stated absence.
              <p className="border-border text-muted-foreground rounded-xl border border-dashed px-5 py-4 text-sm">
                {t("profile.stats.hidden")}
              </p>
            )}
          </>
        )}
      </QueryState>

      {ratings.data !== undefined && <RatingCards ratings={ratings.data.ratings} />}

      <TournamentHistory playerId={profile.data?.id} />
    </div>
  );
}
