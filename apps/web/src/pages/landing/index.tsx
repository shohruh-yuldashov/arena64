import { Link } from "@tanstack/react-router";
import {
  ClockIcon,
  HistoryIcon,
  SmileIcon,
  SwordsIcon,
  TrendingUpIcon,
  TrophyIcon,
  UserPlusIcon,
} from "lucide-react";

import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui";
import { BoardShowcase } from "@/widgets/marketing/board-showcase";
import { BracketShowcase } from "@/widgets/marketing/bracket-showcase";
import { PublicShell } from "@/widgets/marketing/public-shell";

/**
 * Arena64's front door — A64-026.1 §40.
 *
 * ## What it has to answer, in order
 *
 * What is this, why play here, how do I start, what can I do, where do I
 * sign up. A visitor who reads only the first screen should already know
 * the answer to the first: this is online draughts, and the picture beside
 * the headline is a board.
 *
 * ## Every claim is checkable
 *
 * §5 forbids inventing a statistic, and a landing page is where that rule
 * is under the most pressure — "12,481 players online" is one line of copy
 * away and would be false the moment it was written. There is no player
 * count, no games-played counter, no testimonial, no logo wall, no award
 * and no leaderboard.
 *
 * Everything named here was verified against the repository before it was
 * written: four time controls seeded by the migration that creates the
 * table, Glicko-2 per speed class, single-elimination brackets, friend
 * challenges, quick messages (a fixed set of phrases — **not** free-text
 * chat, which this product does not have), replay of every finished game,
 * and three languages including the mail.
 *
 * ## Rhythm, not a stack of card grids
 *
 * Six sections and no two built the same way: a split hero, a three-step
 * flow, a copy-beside-visual competitive section, a wide bracket, a compact
 * social grid, and a single closing statement. §16 of the brief is the
 * reason — "heading + paragraph + three cards" repeated six times is a
 * template, and a visitor recognises one.
 */
export default function LandingPage() {
  // The frame — skip link, header, footer — is `PublicShell`'s since
  // A64-026.4 §43.5, shared with the pages a visitor without an account can
  // now also read. This page is its six sections and nothing else.
  return (
    <PublicShell>
      <Hero />
      <HowItWorks />
      <Competitive />
      <Tournaments />
      <Social />
      <ClosingCta />
    </PublicShell>
  );
}

/** A section shell: one max-width, one rhythm, set in one place. */
function Section({
  id,
  className,
  children,
}: {
  id?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      {...(id ? { id, "aria-labelledby": `${id}-heading` } : {})}
      className={cn("mx-auto w-full max-w-6xl px-4 py-14 sm:py-20", className)}
    >
      {children}
    </section>
  );
}

/**
 * The first screen — §40.1.
 *
 * Asymmetric on desktop: copy left, board right, because the board is the
 * argument and a centred hero would make it decoration. It stacks on a
 * phone with the copy first, so the headline and the button are above the
 * fold and the picture is the reward for scrolling one line.
 *
 * The `<h1>` says what the product is rather than repeating the wordmark
 * the header carries three elements above. "Arena64" as the only heading
 * tells a visitor the name of a thing whose purpose they have not been
 * given.
 */
function Hero() {
  const { t } = useTranslation();

  return (
    <div className="relative overflow-hidden">
      {/* The brand motif: a checker grid, at the lowest contrast that still
          reads, fading out before it reaches the copy. §22 of the brief —
          subtle, brand-coloured, decorative, and hidden from assistive
          technology because it says nothing. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 [background-image:linear-gradient(var(--primary)_1px,transparent_1px),linear-gradient(90deg,var(--primary)_1px,transparent_1px)] [background-size:56px_56px] opacity-[0.04] [mask-image:radial-gradient(120%_80%_at_70%_0%,black,transparent_70%)]"
      />

      <div className="relative mx-auto grid w-full max-w-6xl items-center gap-10 px-4 py-14 sm:py-20 lg:grid-cols-[1.05fr_0.95fr] lg:gap-14 lg:py-24">
        <div className="flex flex-col items-start gap-6">
          <span className="border-border bg-card text-muted-foreground inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium">
            {t("landing.hero.eyebrow")}
          </span>

          <h1 className="text-4xl leading-[1.08] font-semibold tracking-tight text-balance sm:text-5xl lg:text-6xl">
            {t("landing.hero.title")}
          </h1>

          <p className="text-muted-foreground max-w-xl text-base leading-relaxed sm:text-lg">
            {t("landing.hero.body")}
          </p>

          <div className="flex w-full flex-col gap-3 sm:w-auto sm:flex-row">
            <Button asChild size="lg" className="min-h-12 sm:min-w-48">
              <Link to="/register">{t("landing.cta.primary")}</Link>
            </Button>
            <Button asChild size="lg" variant="outline" className="min-h-12">
              <Link to="/login">{t("auth.login.submit")}</Link>
            </Button>
          </div>

          <p className="text-muted-foreground text-xs">{t("landing.hero.free")}</p>
        </div>

        {/* The board leads on desktop and follows on a phone, which is why
            it is ordered rather than positioned. */}
        <div className="w-full max-w-md justify-self-center lg:max-w-none">
          <BoardShowcase />
        </div>
      </div>
    </div>
  );
}

/** The three steps between arriving and playing. All of them the real flow. */
const STEPS: readonly { title: TranslationKey; body: TranslationKey }[] = [
  { title: "landing.steps.one.title", body: "landing.steps.one.body" },
  { title: "landing.steps.two.title", body: "landing.steps.two.body" },
  { title: "landing.steps.three.title", body: "landing.steps.three.body" },
];

function HowItWorks() {
  const { t } = useTranslation();

  return (
    <Section id="play">
      <div className="flex flex-col gap-3">
        <h2
          id="play-heading"
          className="text-2xl font-semibold tracking-tight text-balance sm:text-3xl"
        >
          {t("landing.steps.title")}
        </h2>
        <p className="text-muted-foreground max-w-2xl text-base">{t("landing.steps.body")}</p>
      </div>

      {/* An ordered list, because the order is the content. A grid of three
          divs says the same thing to a sighted reader and nothing at all to
          somebody using a screen reader. */}
      <ol className="mt-8 grid gap-4 sm:grid-cols-3">
        {STEPS.map((step, index) => (
          <li
            key={step.title}
            className="border-border bg-card relative flex flex-col gap-2 rounded-2xl border p-6"
          >
            {/* Decorative: the list already announces the position, and
                reading it twice is worse than not styling it. */}
            <span
              aria-hidden="true"
              className="text-primary/25 absolute top-4 right-5 text-4xl font-semibold tabular-nums"
            >
              {index + 1}
            </span>
            <h3 className="pr-10 text-lg font-medium">{t(step.title)}</h3>
            <p className="text-muted-foreground text-sm leading-relaxed">{t(step.body)}</p>
          </li>
        ))}
      </ol>
    </Section>
  );
}

/**
 * What "competitive" means here — §40.5.
 *
 * Copy beside a list rather than three cards, because the four facts are
 * not peers: the rating is the claim and the rest support it. The time
 * controls are named individually because they are the most concrete thing
 * on the page and the easiest to check.
 */
const COMPETITIVE: readonly { icon: typeof ClockIcon; label: TranslationKey }[] = [
  { icon: TrendingUpIcon, label: "landing.compete.points.rating" },
  { icon: ClockIcon, label: "landing.compete.points.controls" },
  { icon: SwordsIcon, label: "landing.compete.points.rated" },
  { icon: HistoryIcon, label: "landing.compete.points.replay" },
];

function Competitive() {
  const { t } = useTranslation();

  return (
    <Section id="compete" className="border-border border-t">
      <div className="grid items-center gap-10 lg:grid-cols-2 lg:gap-16">
        <div className="flex flex-col gap-5">
          <h2
            id="compete-heading"
            className="text-2xl font-semibold tracking-tight text-balance sm:text-3xl"
          >
            {t("landing.compete.title")}
          </h2>
          <p className="text-muted-foreground max-w-xl text-base leading-relaxed">
            {t("landing.compete.body")}
          </p>

          <ul className="flex flex-col gap-3">
            {COMPETITIVE.map((point) => {
              const Icon = point.icon;
              return (
                <li key={point.label} className="flex items-start gap-3">
                  <span className="bg-primary/10 text-primary mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg">
                    <Icon aria-hidden="true" className="size-4" />
                  </span>
                  <span className="text-sm leading-relaxed">{t(point.label)}</span>
                </li>
              );
            })}
          </ul>
        </div>

        {/* The four time controls, named. The only place on this page with a
            number in it, and every one of them is a row in the database. */}
        <ul className="grid grid-cols-2 gap-3">
          {CONTROLS.map((control) => (
            <li
              key={control.clock}
              className="border-border bg-card flex flex-col gap-1 rounded-xl border p-5"
            >
              <span className="text-2xl font-semibold tracking-tight tabular-nums">
                {control.clock}
              </span>
              <span className={cn("text-sm font-medium", control.accent)}>
                {t(control.label)}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </Section>
  );
}

/**
 * The seeded catalogue, written out.
 *
 * The accent classes are literals from `speed-accent`'s vocabulary rather
 * than a call to it: that helper maps a server-supplied speed class, and
 * there is no server here. Writing the four names as literals is what keeps
 * Tailwind able to see them — an interpolated `text-speed-${x}` generates
 * nothing.
 */
const CONTROLS = [
  {
    clock: "1+0",
    label: "play.speed.bullet" as TranslationKey,
    accent: "text-speed-bullet",
  },
  {
    clock: "3+2",
    label: "play.speed.blitz" as TranslationKey,
    accent: "text-speed-blitz",
  },
  {
    clock: "10+0",
    label: "play.speed.rapid" as TranslationKey,
    accent: "text-speed-rapid",
  },
  {
    clock: "30+0",
    label: "play.speed.classical" as TranslationKey,
    accent: "text-speed-classical",
  },
] as const;

/** The bracket, given the width it needs — §40.6. */
function Tournaments() {
  const { t } = useTranslation();

  return (
    <Section id="tournaments" className="border-border border-t">
      <div className="grid items-center gap-10 lg:grid-cols-[0.9fr_1.1fr] lg:gap-16">
        <div className="flex flex-col gap-5">
          <span className="bg-primary/10 text-primary inline-flex w-fit items-center gap-2 rounded-full px-3 py-1 text-xs font-medium">
            <TrophyIcon aria-hidden="true" className="size-3.5" />
            {t("landing.tournaments.eyebrow")}
          </span>
          <h2
            id="tournaments-heading"
            className="text-2xl font-semibold tracking-tight text-balance sm:text-3xl"
          >
            {t("landing.tournaments.title")}
          </h2>
          <p className="text-muted-foreground max-w-xl text-base leading-relaxed">
            {t("landing.tournaments.body")}
          </p>
        </div>

        <BracketShowcase className="w-full" />
      </div>
    </Section>
  );
}

/**
 * Friends, challenges and quick messages — §40.7.
 *
 * The third item is the one worth being careful about. Arena64 has **quick
 * messages**: a fixed set of phrases a player picks from, with a spam rule.
 * It does not have free-text chat, and copy saying "chat with your friends"
 * would be a feature this product deliberately does not ship.
 */
const SOCIAL: readonly {
  icon: typeof UserPlusIcon;
  title: TranslationKey;
  body: TranslationKey;
}[] = [
  {
    icon: UserPlusIcon,
    title: "landing.social.friends.title",
    body: "landing.social.friends.body",
  },
  {
    icon: SwordsIcon,
    title: "landing.social.challenge.title",
    body: "landing.social.challenge.body",
  },
  {
    icon: SmileIcon,
    title: "landing.social.messages.title",
    body: "landing.social.messages.body",
  },
];

function Social() {
  const { t } = useTranslation();

  return (
    <Section id="social" className="border-border border-t">
      <div className="flex flex-col gap-3">
        <h2
          id="social-heading"
          className="text-2xl font-semibold tracking-tight text-balance sm:text-3xl"
        >
          {t("landing.social.title")}
        </h2>
        <p className="text-muted-foreground max-w-2xl text-base">{t("landing.social.body")}</p>
      </div>

      <ul className="mt-8 grid gap-4 sm:grid-cols-3">
        {SOCIAL.map((item) => {
          const Icon = item.icon;
          return (
            <li
              key={item.title}
              className="border-border bg-card flex flex-col gap-3 rounded-2xl border p-6"
            >
              <span className="bg-muted text-muted-foreground flex size-10 items-center justify-center rounded-xl">
                <Icon aria-hidden="true" className="size-5" />
              </span>
              <h3 className="text-lg font-medium">{t(item.title)}</h3>
              <p className="text-muted-foreground text-sm leading-relaxed">{t(item.body)}</p>
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

/**
 * The last ask — §40.8.
 *
 * Deliberately not the hero again: a visitor who reached here has read the
 * argument, so this is a statement and one button rather than a second
 * pitch. The brand gradient is on the panel, which is the third of the
 * three places §18.7 grants it — the wordmark, the primary button, and a
 * brand surface.
 */
function ClosingCta() {
  const { t } = useTranslation();

  return (
    <Section className="pb-16 sm:pb-24">
      <div className="brand-gradient relative overflow-hidden rounded-3xl px-6 py-14 text-center sm:px-12 sm:py-20">
        {/* The same checker grid as the hero, inverted onto the brand
            surface so the two ends of the page rhyme. */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 [background-image:linear-gradient(#fff_1px,transparent_1px),linear-gradient(90deg,#fff_1px,transparent_1px)] [background-size:56px_56px] opacity-[0.07]"
        />

        <div className="relative mx-auto flex max-w-2xl flex-col items-center gap-5">
          {/* Both gradient stops clear 4.5:1 against this foreground — the
              rule §18.7 states, and the reason the magenta end is darker
              than a magenta wants to be. */}
          <h2 className="text-primary-foreground text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
            {t("landing.closing.title")}
          </h2>
          <p className="text-primary-foreground/85 text-base">{t("landing.closing.body")}</p>
          <Button
            asChild
            size="lg"
            variant="secondary"
            className="min-h-12 w-full sm:w-auto sm:min-w-52"
          >
            <Link to="/register">{t("landing.cta.primary")}</Link>
          </Button>
        </div>
      </div>
    </Section>
  );
}
