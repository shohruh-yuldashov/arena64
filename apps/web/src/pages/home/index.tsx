import { Link } from "@tanstack/react-router";
import { HistoryIcon, SwordsIcon, TrophyIcon, UsersIcon } from "lucide-react";

import { primaryRating, winRateLabel } from "@/entities/profile";
import { isAuthenticated } from "@/entities/session";
import { speedClassKey } from "@/entities/time-control";
import { displayNameOf } from "@/entities/user";
import { useSession } from "@/features/auth/model/session-provider";
import { useMyProfile, useMyRatings } from "@/features/profile/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatNumber, formatPercent } from "@/shared/lib/format";
import { speedAccent } from "@/shared/lib/speed-accent";
import { Button } from "@/shared/ui";
import { BoardMotif } from "@/widgets/board-motif";

/**
 * Arena64's product home — A64-025.3 §2, §3 and A64-025.9B §19.
 *
 * ## What this is, and what it is not
 *
 * The **product home of a signed-in player**, not a marketing landing page.
 * That distinction is A64-025.3 §2's and it closes OQ-1: a public landing
 * page is a separate surface for a separate audience, and building one here
 * would be guessing at copy nobody has written.
 *
 * ## The one rule that changed
 *
 * §3 forbade any query here, on the grounds that a dashboard would have to
 * invent its figures — an online-player count, a recommended tournament —
 * and a plausible number the server never sent is worse than an empty page.
 * That reasoning stands and nothing invented has been added.
 *
 * What has been added is **the player's own standing**, which is not
 * invented: `/profile/me` and `/ratings/me` are the same two requests
 * `/profile` makes, they are already in the cache on any second visit, and
 * the header reads the first of them anyway for the avatar. A home page
 * that cannot tell a returning player how strong they are is not being
 * disciplined; it is being empty.
 *
 * The strip renders only once both have arrived, and never renders a
 * skeleton: it is a detail beside the call to action, and a placeholder
 * that pushes the primary button down the page for 200ms is worse than a
 * strip that appears.
 *
 * ## The guard stays on the route
 *
 * `/` is deliberately **not** wrapped in `protectedPage`, and this page does
 * not reimplement one. A64-025.3 §2 says to preserve the existing guard
 * semantics, and the existing semantics are that `/` is open — so an
 * anonymous visitor gets a signed-out home rather than a redirect.
 */

/** The four places a player goes from home, all of them existing routes. */
const DESTINATIONS: readonly {
  to: string;
  title: TranslationKey;
  body: TranslationKey;
  icon: typeof TrophyIcon;
}[] = [
  {
    to: "/tournaments",
    title: "tournament.nav",
    body: "home.tournamentsBody",
    icon: TrophyIcon,
  },
  {
    to: "/challenges",
    title: "social.nav.challenges",
    body: "home.challengesBody",
    icon: SwordsIcon,
  },
  { to: "/friends", title: "social.nav.friends", body: "home.friendsBody", icon: UsersIcon },
  { to: "/games/history", title: "history.title", body: "home.historyBody", icon: HistoryIcon },
];

export default function HomePage() {
  const { t } = useTranslation();
  const { state } = useSession();
  const signedIn = isAuthenticated(state);

  return (
    <div className="flex flex-col gap-8">
      {/* A floor under the hero so the two session states are the same
          shape: signed out has two lines and two buttons where signed in
          has three and one, and without it the anonymous card was squat
          enough to cut the motif through the middle of a square. */}
      <section className="border-border bg-card relative overflow-hidden rounded-xl border sm:min-h-60">
        {/* Bleeding off the corner rather than sitting in a column of its
            own: the art is the background of the section, so the text keeps
            the full width it needs in every language and the motif never
            competes with the call to action for space. Hidden below `sm`,
            where 360px has none to spare.

            At full strength, not faded. The board's own tokens are a muted
            warm beige to begin with, so it sits behind the text without
            being ghosted — and a product showing its own board apologetically
            at 18% opacity looks like a rendering fault, which is what the
            first attempt looked like. */}
        <BoardMotif className="pointer-events-none absolute -right-8 -bottom-10 hidden size-52 rotate-12 sm:block lg:size-60" />

        <div className="relative flex flex-col items-start gap-5 p-6 sm:p-8">
          <div className="flex flex-col gap-2">
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              {signedIn
                ? t("home.greeting", { name: displayNameOf(state.user) })
                : t("layout.title")}
            </h1>
            <p className="text-muted-foreground max-w-prose text-sm">
              {t(signedIn ? "home.subtitle" : "layout.description")}
            </p>
          </div>

          {signedIn && <StandingStrip />}

          {/* The one thing this page exists to answer. First in the DOM
              after the heading and the only `default` variant on the page,
              sized so it is the largest target on any viewport — §3's "one
              to two seconds" is a layout requirement, not a copy one. */}
          {signedIn ? (
            <Button asChild size="lg" className="min-h-12 w-full sm:w-auto sm:min-w-44">
              <Link to="/play">{t("home.playCta")}</Link>
            </Button>
          ) : (
            <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
              <Button asChild size="lg" className="min-h-12 sm:min-w-40">
                <Link to="/register">{t("auth.register.submit")}</Link>
              </Button>
              <Button asChild size="lg" variant="outline" className="min-h-12">
                <Link to="/login">{t("auth.login.submit")}</Link>
              </Button>
            </div>
          )}
        </div>
      </section>

      {/* Only for a signed-in player: every destination below is behind the
          verified-email guard, and offering a card that redirects to sign-in
          is a link that lies about where it goes. */}
      {signedIn && (
        <section aria-labelledby="home-more" className="flex flex-col gap-4">
          <h2 id="home-more" className="text-lg font-semibold tracking-tight">
            {t("home.moreTitle")}
          </h2>
          <ul className="grid gap-3 sm:grid-cols-2">
            {DESTINATIONS.map((destination) => {
              const Icon = destination.icon;
              return (
                <li key={destination.to}>
                  {/* `group` on the card and `after:inset-0` on the link:
                      the whole card is the click target, and there is still
                      exactly **one** link with the section's name as its
                      accessible name. A card-sized anchor wrapped round a
                      heading reads as one unlabelled link to a screen
                      reader; a second nested link reads as two. */}
                  <div className="group border-border bg-card hover:border-primary/40 relative flex h-full items-start gap-4 rounded-xl border p-5 transition-colors duration-fast">
                    <span className="bg-muted text-muted-foreground group-hover:bg-primary/10 group-hover:text-primary flex size-10 shrink-0 items-center justify-center rounded-lg transition-colors duration-fast">
                      <Icon aria-hidden="true" className="size-5" />
                    </span>
                    <div className="flex min-w-0 flex-col gap-1">
                      <h3 className="text-sm font-semibold">
                        <Link
                          to={destination.to}
                          className="focus-visible:ring-ring rounded-sm after:absolute after:inset-0 focus-visible:ring-2 focus-visible:outline-none"
                        >
                          {t(destination.title)}
                        </Link>
                      </h3>
                      <p className="text-muted-foreground text-sm">{t(destination.body)}</p>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}

/**
 * The player's leading standing and record, in one line.
 *
 * Every figure is the server's: `primaryRating` picks which of the returned
 * categories to lead with — the most played, never the highest, for the
 * reason recorded on that function — and `winRateLabel` is what keeps a
 * player with no games from being told they lost everything.
 *
 * Renders nothing until both requests have landed, and nothing at all for
 * an account that has not played: a row of dashes is not information.
 */
function StandingStrip() {
  const { t, locale } = useTranslation();
  const profile = useMyProfile();
  const ratings = useMyRatings();

  const statistics = profile.data?.statistics;
  const standing = ratings.data === undefined ? null : primaryRating(ratings.data.ratings);
  if (statistics === undefined || standing === null) return null;

  const rate = winRateLabel(statistics);
  const accent = speedAccent(standing.speed_class);

  return (
    <dl className="text-muted-foreground flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
      <div className="flex items-baseline gap-2">
        <dt className={cn("text-xs font-semibold", accent.text)}>
          {t(speedClassKey(standing.speed_class))}
        </dt>
        <dd className="text-foreground text-lg leading-none font-semibold tabular-nums">
          {formatNumber(Math.round(standing.rating), locale)}
        </dd>
      </div>

      <div className="flex items-baseline gap-2">
        <dt className="text-xs">{t("profile.stats.gamesPlayed")}</dt>
        <dd className="text-foreground font-medium tabular-nums">
          {formatNumber(statistics.games_played, locale)}
        </dd>
      </div>

      {rate !== null && (
        <div className="flex items-baseline gap-2">
          <dt className="text-xs">{t("profile.stats.winRate")}</dt>
          <dd className="text-foreground font-medium tabular-nums">
            {formatPercent(rate, locale)}
          </dd>
        </div>
      )}
    </dl>
  );
}
