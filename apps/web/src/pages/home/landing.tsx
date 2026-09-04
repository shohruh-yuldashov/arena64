import { Link } from "@tanstack/react-router";
import {
  ClockIcon,
  GlobeIcon,
  HistoryIcon,
  SwordsIcon,
  TrendingUpIcon,
  TrophyIcon,
} from "lucide-react";

import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui";
import { BoardMotif } from "@/widgets/board-motif";

/**
 * What a visitor who is not signed in sees at `/` — A64-026.1 §39.
 *
 * ## Why it lives beside the product home rather than at its own route
 *
 * `/` is open, and A64-025.3 §2 kept it that way: an anonymous visitor gets
 * a signed-out home rather than a redirect. So this is not a second route
 * competing with `/` — it **is** `/`, for the half of its audience that has
 * no account. `pages/home` chooses between the two and nothing else does.
 *
 * A dedicated `/about` would be a page nothing links to, and a landing page
 * behind a link is a landing page nobody lands on.
 *
 * ## Every claim here is one the product can keep
 *
 * `specs/product-experience.md` §5 forbids inventing a statistic, and a
 * landing page is where that rule is under the most pressure: "10,000
 * players" is one line of copy away and would be a lie the moment it was
 * written. So there is no player count, no games-played counter, no
 * testimonial, and no logo wall.
 *
 * What is here instead is **six things the built product does**, each one
 * checkable by opening the app: four time controls seeded by a migration,
 * Glicko-2 per speed class, single-elimination tournaments with a real
 * bracket, friend challenges, a replay of every finished game, and three
 * languages. A visitor who signs up finds exactly that.
 *
 * ## No marketing theme
 *
 * A64-026's brief allows a dedicated one, and this does not take it. The
 * product already has a token system, a rationed brand gradient (§18.7) and
 * a motion scale (§34); a second palette that applies to one page would be
 * a second thing to keep in step, and the first divergence would be the
 * moment a visitor crossed from here into `/register`.
 */
export function LandingPage() {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-10 py-2 sm:gap-14">
      <Hero />
      <HowItWorks />
      <Features />

      {/* The second ask, for somebody who scrolled because the first one
          did not convince them. Same destination, and deliberately the only
          other `default` button on the page. */}
      <section className="border-border bg-card flex flex-col items-center gap-4 rounded-2xl border px-6 py-10 text-center">
        <h2 className="text-xl font-semibold tracking-tight sm:text-2xl">
          {t("landing.closing.title")}
        </h2>
        <p className="text-muted-foreground max-w-prose text-sm">{t("landing.closing.body")}</p>
        <Button asChild size="lg" className="min-h-12 w-full sm:w-auto sm:min-w-48">
          <Link to="/register">{t("landing.hero.register")}</Link>
        </Button>
      </section>
    </div>
  );
}

/**
 * The first screen.
 *
 * The heading says what this is rather than repeating the wordmark — the
 * header already carries that four elements above. "Arena64" as an `<h1>`
 * tells a visitor the name of a thing they have not yet been told the
 * purpose of, and a search engine the same.
 */
function Hero() {
  const { t } = useTranslation();

  return (
    <section className="border-border bg-card relative overflow-hidden rounded-2xl border">
      {/* The product's own board, at full strength rather than ghosted —
          A64-025.3's reasoning: a board shown apologetically at 18% opacity
          reads as a rendering fault. Hidden below `sm`, where it would take
          the room the call to action needs. */}
      <BoardMotif className="pointer-events-none absolute -right-10 -bottom-12 hidden size-56 rotate-12 sm:block lg:size-72" />

      <div className="relative flex max-w-2xl flex-col items-start gap-5 p-6 sm:p-10">
        <h1 className="text-3xl leading-tight font-semibold tracking-tight sm:text-4xl">
          {t("landing.hero.title")}
        </h1>
        <p className="text-muted-foreground max-w-prose text-base">{t("landing.hero.body")}</p>

        <div className="flex w-full flex-col gap-2 pt-1 sm:w-auto sm:flex-row">
          <Button asChild size="lg" className="min-h-12 sm:min-w-44">
            <Link to="/register">{t("landing.hero.register")}</Link>
          </Button>
          <Button asChild size="lg" variant="outline" className="min-h-12">
            <Link to="/login">{t("auth.login.submit")}</Link>
          </Button>
        </div>

        <p className="text-muted-foreground text-xs">{t("landing.hero.free")}</p>
      </div>
    </section>
  );
}

/** The three steps between arriving and playing. All of them real. */
const STEPS: readonly { title: TranslationKey; body: TranslationKey }[] = [
  { title: "landing.steps.one.title", body: "landing.steps.one.body" },
  { title: "landing.steps.two.title", body: "landing.steps.two.body" },
  { title: "landing.steps.three.title", body: "landing.steps.three.body" },
];

function HowItWorks() {
  const { t } = useTranslation();

  return (
    <section aria-labelledby="landing-steps" className="flex flex-col gap-5">
      <h2 id="landing-steps" className="text-xl font-semibold tracking-tight sm:text-2xl">
        {t("landing.steps.title")}
      </h2>

      {/* An ordered list, because the order is the content. A grid of three
          divs would say the same thing to a sighted reader and nothing to
          anybody using a screen reader. */}
      <ol className="grid gap-4 sm:grid-cols-3">
        {STEPS.map((step, index) => (
          <li
            key={step.title}
            className="border-border bg-card flex flex-col gap-2 rounded-xl border p-5"
          >
            {/* The number is decorative — the list already announces the
                position — so it is hidden rather than read out twice. */}
            <span
              aria-hidden="true"
              className="bg-primary/10 text-primary flex size-8 items-center justify-center rounded-full text-sm font-semibold tabular-nums"
            >
              {index + 1}
            </span>
            <h3 className="text-base font-medium">{t(step.title)}</h3>
            <p className="text-muted-foreground text-sm">{t(step.body)}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * Six things the product does, and nothing it does not.
 *
 * Each one is checkable by opening the app, which is the test a claim on
 * this page has to pass. The icons are decoration beside a sentence that
 * already says it — `aria-hidden`, per the same rule `ListState`'s empty
 * state follows.
 */
const FEATURES: readonly {
  icon: typeof ClockIcon;
  title: TranslationKey;
  body: TranslationKey;
}[] = [
  {
    icon: ClockIcon,
    title: "landing.features.realtime.title",
    body: "landing.features.realtime.body",
  },
  {
    icon: TrendingUpIcon,
    title: "landing.features.rating.title",
    body: "landing.features.rating.body",
  },
  {
    icon: TrophyIcon,
    title: "landing.features.tournaments.title",
    body: "landing.features.tournaments.body",
  },
  {
    icon: SwordsIcon,
    title: "landing.features.friends.title",
    body: "landing.features.friends.body",
  },
  {
    icon: HistoryIcon,
    title: "landing.features.replay.title",
    body: "landing.features.replay.body",
  },
  {
    icon: GlobeIcon,
    title: "landing.features.languages.title",
    body: "landing.features.languages.body",
  },
];

function Features() {
  const { t } = useTranslation();

  return (
    <section aria-labelledby="landing-features" className="flex flex-col gap-5">
      <h2 id="landing-features" className="text-xl font-semibold tracking-tight sm:text-2xl">
        {t("landing.features.title")}
      </h2>

      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map((feature) => {
          const Icon = feature.icon;
          return (
            <li
              key={feature.title}
              className="border-border bg-card flex flex-col gap-2 rounded-xl border p-5"
            >
              <span className="bg-muted text-muted-foreground flex size-9 items-center justify-center rounded-lg">
                <Icon aria-hidden="true" className="size-4.5" />
              </span>
              <h3 className="text-base font-medium">{t(feature.title)}</h3>
              <p className="text-muted-foreground text-sm">{t(feature.body)}</p>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
