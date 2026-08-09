import { Link } from "@tanstack/react-router";

import { isAuthenticated } from "@/entities/session";
import { displayNameOf } from "@/entities/user";
import { useSession } from "@/features/auth/model/session-provider";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/shared/ui";

/**
 * Arena64's product home — A64-025.3 §2, §3.
 *
 * ## What was here
 *
 * A foundation exhibit from A64-018: a heading, the sentence "Application
 * shell. No gameplay surface is built yet.", a card demonstrating `Skeleton`
 * and `Spinner`, and a `FormDemo`. It was the only page in the app with no
 * translations, which is what made it obvious it was never meant to survive
 * — its own docstring said A64-020.5 would replace it. A64-020.5 built
 * `/play` and left it, so for five phases the first screen every visitor
 * saw was the product telling them it did not exist
 * (`specs/product-experience.md` P0-1).
 *
 * ## What this is, and what it is not
 *
 * The **product home of a signed-in player**, not a marketing landing page.
 * That distinction is A64-025.3 §2's and it closes OQ-1: a public landing
 * page is a separate surface for a separate audience, and building one here
 * would be guessing at copy nobody has written.
 *
 * ## No new data
 *
 * Every word and every destination on this page comes from the session that
 * is already loaded and from routes that already exist. §3 forbids inventing
 * a dashboard — an online-player count, a win streak, a recommended
 * tournament — because none of those is a number this backend offers, and a
 * plausible-looking figure the server never sent is worse than an empty
 * page. So there is no query here, and therefore no loading and no error
 * state to design (§16).
 *
 * ## The guard stays on the route
 *
 * `/` is deliberately **not** wrapped in `protectedPage`, and this page does
 * not reimplement one. A64-025.3 §2 says to preserve the existing guard
 * semantics, and the existing semantics are that `/` is open — so an
 * anonymous visitor gets a signed-out home rather than a redirect. Reading
 * `useSession` to decide what to render is what `AppShell` already does for
 * `MatchOfferSurface`; it is not an authorization decision and nothing here
 * is protected by it.
 */

/** The four places a player goes from home, all of them existing routes. */
const DESTINATIONS: readonly { to: string; title: TranslationKey; body: TranslationKey }[] = [
  { to: "/tournaments", title: "tournament.nav", body: "home.tournamentsBody" },
  { to: "/challenges", title: "social.nav.challenges", body: "home.challengesBody" },
  { to: "/friends", title: "social.nav.friends", body: "home.friendsBody" },
  { to: "/games/history", title: "history.title", body: "home.historyBody" },
];

export default function HomePage() {
  const { t } = useTranslation();
  const { state } = useSession();
  const signedIn = isAuthenticated(state);

  return (
    <div className="flex flex-col gap-8">
      <section className="flex flex-col items-start gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            {signedIn
              ? t("home.greeting", { name: displayNameOf(state.user) })
              : t("layout.title")}
          </h1>
          <p className="text-muted-foreground text-sm">
            {t(signedIn ? "home.subtitle" : "layout.description")}
          </p>
        </div>

        {/* The one thing this page exists to answer. First in the DOM after
            the heading, the only `default` variant on the page, and sized
            so it is the largest target on any viewport — §3's "one to two
            seconds" is a layout requirement, not a copy one. */}
        {signedIn ? (
          <Button asChild size="lg" className="min-h-12 w-full sm:w-auto">
            <Link to="/play">{t("home.playCta")}</Link>
          </Button>
        ) : (
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
            <Button asChild size="lg" className="min-h-12">
              <Link to="/login">{t("auth.login.submit")}</Link>
            </Button>
            <Button asChild size="lg" variant="outline" className="min-h-12">
              <Link to="/register">{t("auth.register.submit")}</Link>
            </Button>
          </div>
        )}
      </section>

      {/* Only for a signed-in player: every destination below is behind the
          verified-email guard, and offering a card that redirects to sign-in
          is a link that lies about where it goes. */}
      {signedIn && (
        <section aria-labelledby="home-more" className="flex flex-col gap-4">
          <h2 id="home-more" className="text-lg font-semibold tracking-tight">
            {t("home.moreTitle")}
          </h2>
          <ul className="grid gap-4 sm:grid-cols-2">
            {DESTINATIONS.map((destination) => (
              <li key={destination.to}>
                <Card className="h-full">
                  <CardHeader>
                    <CardTitle className="text-base">
                      {/* The whole card is not the link: a card-sized click
                          target with a heading inside it reads as one
                          unlabelled link to a screen reader. The title is
                          the link, and it is what gets announced. */}
                      <Link
                        to={destination.to}
                        className="focus-visible:ring-ring rounded-sm hover:underline focus-visible:ring-2 focus-visible:outline-none"
                      >
                        {t(destination.title)}
                      </Link>
                    </CardTitle>
                    <CardDescription>{t(destination.body)}</CardDescription>
                  </CardHeader>
                  <CardContent />
                </Card>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
