import type { ProfileStatistics } from "@/entities/profile";
import { winRateLabel } from "@/entities/profile";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatNumber, formatPercent } from "@/shared/lib/format";

/**
 * A player's match record.
 *
 * **Nothing is computed here.** `games_played`, `wins`, `losses`, `draws`,
 * `win_rate`, `current_rating`, `highest_rating` and the two streaks all
 * come from `statistics`, which owns them. A second implementation of "win
 * rate" in the client is a second answer to the same question, and the two
 * disagree the first time a draw is counted differently.
 *
 * The one presentation decision: a player with no games shows "no games
 * yet" rather than `0%`, which reads as having lost everything.
 *
 * ## Three sizes, not seven of the same — A64-025.9
 *
 * This was seven `text-sm` figures in a flat grid, which means a reader has
 * to read all seven to find out that six of them are subordinate to one.
 * Games, win rate and best streak are what a record *is*, so they are set
 * large; wins, losses and draws are the decomposition of the first of
 * those, so they are drawn as one proportional bar with its own legend; and
 * the highest rating is a footnote, so it is set as one.
 *
 * The bar is decoration over numbers that are already written out beside
 * it — hence `aria-hidden`. It is never the only place a figure appears,
 * which is the same rule the presence dot follows (WCAG 1.4.1).
 */
export function StatisticsPanel({ statistics }: { statistics: ProfileStatistics }) {
  const { t, locale } = useTranslation();
  const rate = winRateLabel(statistics);

  const headline: { label: string; value: string; cell?: string; label_accent?: string }[] = [
    {
      label: t("profile.stats.gamesPlayed"),
      value: formatNumber(statistics.games_played, locale),
    },
    {
      label: t("profile.stats.winRate"),
      value: rate === null ? "—" : formatPercent(rate, locale),
    },
    {
      label: t("profile.stats.bestWinStreak"),
      value: formatNumber(statistics.best_win_streak, locale),
      // The one achievement among the three — §18.7. Games and win rate
      // describe a record; a best streak is a personal high, and `--rating`
      // is the token that means that. The tint sits behind the figure
      // rather than in it: an amber dark enough to clear 4.5:1 on white is
      // brown by the time it gets there, and a brown numeral reads as a
      // mistake. Never the only signal either way — the label says what it
      // is (WCAG 1.4.1).
      cell: statistics.best_win_streak > 0 ? "bg-rating/8" : undefined,
      label_accent: statistics.best_win_streak > 0 ? "text-rating" : undefined,
    },
  ];

  // The bar's denominator, and its own — not `games_played`. An outcome the
  // projection has not classified would otherwise leave a gap that reads as
  // a fourth, unlabelled result.
  const decided = statistics.wins + statistics.losses + statistics.draws;
  const outcomes: { label: string; value: number; bar: string; dot: string }[] = [
    {
      label: t("profile.stats.wins"),
      value: statistics.wins,
      bar: "bg-success",
      dot: "bg-success",
    },
    {
      label: t("profile.stats.losses"),
      value: statistics.losses,
      bar: "bg-destructive",
      dot: "bg-destructive",
    },
    {
      label: t("profile.stats.draws"),
      value: statistics.draws,
      bar: "bg-muted-foreground/50",
      dot: "bg-muted-foreground/50",
    },
  ];

  return (
    <section aria-labelledby="statistics-heading" className="flex flex-col gap-3">
      <h2 id="statistics-heading" className="text-base font-semibold">
        {t("profile.stats.title")}
      </h2>

      <div className="border-border bg-card overflow-hidden rounded-xl border">
        <dl className="divide-border grid grid-cols-3 divide-x">
          {headline.map((figure) => (
            // `justify-between` in a full-height cell, so the three figures
            // sit on one baseline: grid cells share a row height, and a
            // label that wraps ("Best win streak" does, at 360px) would
            // otherwise push its own number down and misalign all three.
            <div
              key={figure.label}
              className={cn(
                "flex h-full flex-col justify-between gap-2 px-4 py-5 sm:px-6",
                figure.cell,
              )}
            >
              <dt className={cn("text-muted-foreground text-xs", figure.label_accent)}>
                {figure.label}
              </dt>
              <dd className="text-2xl font-semibold tracking-tight tabular-nums sm:text-3xl">
                {figure.value}
              </dd>
            </div>
          ))}
        </dl>

        <div className="border-border flex flex-col gap-3 border-t px-4 py-5 sm:px-6">
          {decided === 0 ? (
            <p className="text-muted-foreground text-sm">{t("profile.stats.noGames")}</p>
          ) : (
            <>
              <div
                aria-hidden="true"
                className="bg-muted flex h-2 overflow-hidden rounded-full"
              >
                {outcomes.map((outcome) => (
                  <div
                    key={outcome.label}
                    className={outcome.bar}
                    style={{ width: `${((outcome.value / decided) * 100).toString()}%` }}
                  />
                ))}
              </div>

              <dl className="flex flex-wrap gap-x-6 gap-y-2">
                {outcomes.map((outcome) => (
                  <div key={outcome.label} className="flex items-center gap-2">
                    <span aria-hidden="true" className={`size-2 rounded-full ${outcome.dot}`} />
                    <dt className="text-muted-foreground text-xs">{outcome.label}</dt>
                    <dd className="text-sm font-medium tabular-nums">
                      {formatNumber(outcome.value, locale)}
                    </dd>
                  </div>
                ))}
              </dl>
            </>
          )}

          <dl className="text-muted-foreground border-border flex gap-2 border-t pt-3 text-xs">
            <dt>{t("profile.stats.highestRating")}</dt>
            <dd className="text-foreground font-medium tabular-nums">
              {formatNumber(statistics.highest_rating, locale)}
            </dd>
          </dl>
        </div>
      </div>
    </section>
  );
}
