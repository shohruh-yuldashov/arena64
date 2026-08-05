import type { ProfileStatistics } from "@/entities/profile";
import { winRateLabel } from "@/entities/profile";
import { useTranslation } from "@/shared/i18n";
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
 */
export function StatisticsPanel({ statistics }: { statistics: ProfileStatistics }) {
  const { t, locale } = useTranslation();
  const rate = winRateLabel(statistics);

  const rows: { label: string; value: string }[] = [
    {
      label: t("profile.stats.gamesPlayed"),
      value: formatNumber(statistics.games_played, locale),
    },
    { label: t("profile.stats.wins"), value: formatNumber(statistics.wins, locale) },
    { label: t("profile.stats.losses"), value: formatNumber(statistics.losses, locale) },
    { label: t("profile.stats.draws"), value: formatNumber(statistics.draws, locale) },
    {
      label: t("profile.stats.winRate"),
      value: rate === null ? t("profile.stats.noGames") : formatPercent(rate, locale),
    },
    {
      label: t("profile.stats.bestWinStreak"),
      value: formatNumber(statistics.best_win_streak, locale),
    },
    {
      label: t("profile.stats.highestRating"),
      value: formatNumber(statistics.highest_rating, locale),
    },
  ];

  return (
    <section aria-labelledby="statistics-heading" className="flex flex-col gap-3">
      <h2 id="statistics-heading" className="text-base font-semibold">
        {t("profile.stats.title")}
      </h2>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
        {rows.map((row) => (
          <div key={row.label} className="flex flex-col">
            <dt className="text-muted-foreground text-xs">{row.label}</dt>
            <dd className="text-sm font-medium tabular-nums">{row.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
