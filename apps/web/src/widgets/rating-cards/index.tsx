import type { PlayerRating } from "@/entities/profile";
import { speedClassKey } from "@/entities/time-control";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatList, formatNumber } from "@/shared/lib/format";
import { speedAccent } from "@/shared/lib/speed-accent";
import { Card, CardContent } from "@/shared/ui";

/**
 * One card per `(variant, speed class)` standing.
 *
 * ## Played and unplayed are not the same kind of thing — A64-025.9
 *
 * `/ratings/me` returns **every** speed class, marking the unplayed ones
 * provisional with zero games. Rendering all five identically gave a new
 * account five cards reading `1,500` — a wall of numbers nobody earned,
 * occupying a third of the page and saying nothing.
 *
 * So the two are separated. A category with games is a measurement and gets
 * a card. A category without them is an *invitation*, and the honest form of
 * that is one line naming them, not five cards pretending to be results.
 *
 * ## What is shown, and the one thing that is not
 *
 * `rating`, `deviation`, `games_played` and `is_provisional`.
 * **`volatility` is never published** — the API does not return it, and it
 * would mean nothing to a reader if it did: it is an input to the next
 * calculation rather than a fact about the player (SPEC-RATING §14.1).
 *
 * Provisional is stated in **text**, not by colour alone (WCAG 1.4.1). The
 * speed class is translated rather than printed raw — it read `blitz`, in
 * every locale, until A64-025.9.
 */
export function RatingCards({ ratings }: { ratings: PlayerRating[] }) {
  const { t, locale } = useTranslation();

  const played = ratings.filter((rating) => rating.games_played > 0);
  const unplayed = ratings.filter((rating) => rating.games_played === 0);

  if (ratings.length === 0) return null;

  return (
    <section aria-labelledby="ratings-heading" className="flex flex-col gap-3">
      <h2 id="ratings-heading" className="text-base font-semibold">
        {t("profile.ratings.title")}
      </h2>

      {played.length > 0 && (
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {played.map((rating) => {
            const accent = speedAccent(rating.speed_class);
            return (
              <li key={`${rating.variant}:${rating.speed_class}`}>
                {/* The rule down the leading edge is the class's own hue —
                    §18.7 — so a returning player finds Blitz by colour
                    before reading the word. Solid, not a gradient: five
                    cards each carrying one would be the "everything is
                    emphasised, so nothing is" the page is built to avoid. */}
                <Card className={cn("h-full gap-0 border-l-4 py-0", accent.border)}>
                  <CardContent className="flex flex-col gap-1 p-5">
                    <span
                      className={cn(
                        "text-xs font-semibold tracking-wide uppercase",
                        accent.text,
                      )}
                    >
                      {t(speedClassKey(rating.speed_class))}
                    </span>
                    <span className="text-3xl leading-none font-semibold tracking-tight tabular-nums">
                      {formatNumber(Math.round(rating.rating), locale)}
                    </span>
                    <span className="text-muted-foreground mt-1 text-xs">
                      {t("profile.ratings.games", { count: rating.games_played })}
                      {rating.is_provisional &&
                        ` · ${t("profile.ratings.provisional")} ±${formatNumber(
                          Math.round(rating.deviation),
                          locale,
                        )}`}
                    </span>
                  </CardContent>
                </Card>
              </li>
            );
          })}
        </ul>
      )}

      {unplayed.length > 0 && (
        <p className="border-border text-muted-foreground rounded-xl border border-dashed px-5 py-4 text-sm">
          {t("profile.ratings.unrated")}
          {" — "}
          {formatList(
            unplayed.map((rating) => t(speedClassKey(rating.speed_class))),
            locale,
          )}
        </p>
      )}
    </section>
  );
}
