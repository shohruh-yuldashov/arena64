import type { PlayerRating } from "@/entities/profile";
import { useTranslation } from "@/shared/i18n";
import { formatNumber } from "@/shared/lib/format";
import { Card, CardContent } from "@/shared/ui";

/**
 * One card per `(variant, speed class)` standing.
 *
 * ## What is shown, and the one thing that is not
 *
 * `rating`, `deviation`, `games_played` and `is_provisional`.
 * **`volatility` is never published** — the API does not return it, and it
 * would mean nothing to a reader if it did: it is an input to the next
 * calculation rather than a fact about the player (SPEC-RATING §14.1).
 *
 * ## No fabricated categories, and no fake 1500
 *
 * `/ratings/me` returns **every** speed class, marking the unplayed ones
 * provisional with zero games — that is the API's own answer, not a
 * default this component invents. A category with no games renders "not
 * rated yet" beside the starting value rather than presenting it as a
 * measurement, because a number nobody earned reads as one they did.
 *
 * Provisional is stated in **text**, not by colour alone (WCAG 1.4.1).
 */
export function RatingCards({ ratings }: { ratings: PlayerRating[] }) {
  const { t, locale } = useTranslation();

  return (
    <section aria-labelledby="ratings-heading" className="flex flex-col gap-3">
      <h2 id="ratings-heading" className="text-base font-semibold">
        {t("profile.ratings.title")}
      </h2>
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {ratings.map((rating) => {
          const unplayed = rating.games_played === 0;
          return (
            <li key={`${rating.variant}:${rating.speed_class}`}>
              <Card className="gap-2 py-4">
                <CardContent className="flex flex-col gap-1 px-4">
                  <span className="text-muted-foreground text-xs tracking-wide uppercase">
                    {rating.speed_class}
                  </span>
                  <span className="text-2xl font-semibold tabular-nums">
                    {formatNumber(Math.round(rating.rating), locale)}
                  </span>
                  <span className="text-muted-foreground text-xs">
                    {unplayed
                      ? t("profile.ratings.unrated")
                      : t("profile.ratings.games", { count: rating.games_played })}
                  </span>
                  {rating.is_provisional && !unplayed && (
                    <span className="text-muted-foreground text-xs font-medium">
                      {t("profile.ratings.provisional")} · ±
                      {formatNumber(Math.round(rating.deviation), locale)}
                    </span>
                  )}
                </CardContent>
              </Card>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
