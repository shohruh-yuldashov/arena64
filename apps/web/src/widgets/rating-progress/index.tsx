import { type KeyboardEvent, type PointerEvent, useState } from "react";

import type { PlayerRating } from "@/entities/profile";
import { primaryRating } from "@/entities/profile";
import { speedClassKey } from "@/entities/time-control";
import type { RatingHistory } from "@/features/profile/api";
import { useMyRatingHistory } from "@/features/profile/model/queries";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatDate, formatNumber, formatRatingChange } from "@/shared/lib/format";
import { speedAccent } from "@/shared/lib/speed-accent";
import { ListState } from "@/shared/ui";

/**
 * The signed-in player's rating over their recent rated games — A64-032.
 *
 * ## Only stored points
 *
 * Every vertex is a `rating_adjustment` the server kept: the line starts at
 * the first listed game's rating *before* it and then steps through each
 * game's rating *after* it. Nothing is interpolated, smoothed or filled in
 * for a quiet month — a gap in play is simply the next game.
 *
 * ## Games along the bottom, not dates
 *
 * Points are evenly spaced by game. On a date axis a player who played ten
 * games on Sunday and one a month later would get a cliff and a smear,
 * which is the axis describing their calendar rather than their rating.
 * The dates are in the readout and the table.
 *
 * ## One speed class at a time
 *
 * A blitz rating and a classical one are two lines, not one line with a
 * jump, so the chart shows one class and offers the others the player has
 * played. It opens on the class with the most games — the same choice the
 * profile header leads with — and draws in that class's own colour.
 *
 * ## Read without the picture
 *
 * The readout above the line says the value at the chosen point; arrow
 * keys move it as the pointer does. The whole series is also a table, so
 * nothing the line shows is available only as geometry.
 *
 * Own profile only. The route behind it answers for the token's player and
 * nobody else.
 */
export function RatingProgress({ ratings }: { ratings: PlayerRating[] }) {
  const { t } = useTranslation();
  const played = ratings.filter((rating) => rating.games_played > 0);
  const [chosen, setChosen] = useState<string | null>(null);
  const speedClass = chosen ?? primaryRating(ratings)?.speed_class ?? null;
  const history = useMyRatingHistory(speedClass);
  const points = history.data?.points ?? [];

  return (
    <section aria-labelledby="rating-progress-heading" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 id="rating-progress-heading" className="text-base font-semibold">
          {t("profile.ratingProgress.title")}
        </h2>

        {played.length > 1 && (
          <div
            role="group"
            aria-label={t("profile.ratingProgress.speedClass")}
            className="bg-muted inline-flex rounded-lg p-0.5"
          >
            {played.map((rating) => (
              <button
                key={rating.speed_class}
                type="button"
                aria-pressed={rating.speed_class === speedClass}
                onClick={() => {
                  setChosen(rating.speed_class);
                }}
                className={cn(
                  "focus-visible:ring-ring min-h-9 rounded-md px-3 text-xs font-medium focus-visible:ring-2 focus-visible:outline-none",
                  rating.speed_class === speedClass
                    ? "bg-card text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t(speedClassKey(rating.speed_class))}
              </button>
            ))}
          </div>
        )}
      </div>

      {speedClass === null ? (
        <p className="border-border text-muted-foreground rounded-xl border border-dashed px-5 py-4 text-sm">
          {t("profile.ratingProgress.none")}
        </p>
      ) : (
        <ListState
          isPending={history.isPending}
          isError={history.isError}
          isEmpty={points.length === 0}
          loadingLabel={t("profile.ratingProgress.loadingLabel")}
          errorMessage={t("profile.ratingProgress.error")}
          emptyTitle={t("profile.ratingProgress.none")}
          pendingRows={1}
          pendingRowClassName="h-56 rounded-xl"
          onRetry={() => void history.refetch()}
        >
          {history.data !== undefined && points.length > 0 && (
            // Keyed by class: a new line starts with its own crosshair.
            <RatingLine key={speedClass} history={history.data} speedClass={speedClass} />
          )}
        </ListState>
      )}
    </section>
  );
}

interface Vertex {
  rating: number;
  /** `null` for the starting point, which is the first game's "before". */
  at: string | null;
  delta: number | null;
}

/** Clean tick values spanning the data, about four of them. */
function ticksFor(low: number, high: number): number[] {
  const span = Math.max(high - low, 1);
  const step = [5, 10, 20, 25, 50, 100, 200, 500].find((size) => span / size <= 4) ?? 1000;
  const first = Math.floor(low / step) * step;
  const last = Math.ceil(high / step) * step;
  const ticks: number[] = [];
  for (let value = first; value <= last; value += step) ticks.push(value);
  return ticks;
}

function RatingLine({ history, speedClass }: { history: RatingHistory; speedClass: string }) {
  const { t, locale } = useTranslation();
  const accent = speedAccent(speedClass);
  const stroke = `var(--speed-${speedClass}, var(--primary))`;

  const vertices: Vertex[] = [
    { rating: history.points[0]?.before ?? 0, at: null, delta: null },
    ...history.points.map((point) => ({
      rating: point.after,
      at: point.at,
      delta: point.delta,
    })),
  ];
  const last = vertices.length - 1;
  const [active, setActive] = useState(last);
  const shown = vertices[Math.min(active, last)] ?? vertices[last];

  const ticks = ticksFor(
    Math.min(...vertices.map((vertex) => vertex.rating)),
    Math.max(...vertices.map((vertex) => vertex.rating)),
  );
  const low = ticks[0] ?? 0;
  const high = ticks[ticks.length - 1] ?? 1;
  // Percentages of the plot box: x by game, y with high values at the top.
  const x = (index: number) => (last === 0 ? 50 : (index / last) * 100);
  const y = (rating: number) => (high === low ? 50 : ((high - rating) / (high - low)) * 100);
  const line = vertices.map((vertex, index) => `${x(index)},${y(vertex.rating)}`).join(" ");

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    if (box.width === 0) return;
    const fraction = Math.min(Math.max((event.clientX - box.left) / box.width, 0), 1);
    setActive(Math.round(fraction * last));
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const next =
      event.key === "ArrowLeft"
        ? active - 1
        : event.key === "ArrowRight"
          ? active + 1
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    if (next === null) return;
    event.preventDefault();
    setActive(Math.min(Math.max(next, 0), last));
  };

  const games = history.points.length;
  const readoutDate =
    shown?.at == null ? t("profile.ratingProgress.start") : formatDate(shown.at, locale);

  return (
    <div className="border-border bg-card flex flex-col gap-3 rounded-xl border px-4 py-4 sm:px-5">
      {/* The value leads, then what moved it and when — the readout a
          tooltip would carry, kept in the layout so it never covers the
          line and never depends on a pointer. */}
      <p aria-live="polite" className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-2xl font-semibold tracking-tight tabular-nums">
          {formatNumber(shown?.rating ?? 0, locale)}
        </span>
        {shown?.delta != null && (
          <span
            className={cn(
              "text-sm font-medium tabular-nums",
              shown.delta > 0 && "text-success",
              shown.delta < 0 && "text-destructive",
              shown.delta === 0 && "text-muted-foreground",
            )}
          >
            {formatRatingChange(shown.delta, locale)}
          </span>
        )}
        <span className="text-muted-foreground text-xs">{readoutDate}</span>
      </p>

      <div className="flex gap-2">
        {/* The y-axis labels, in the reader's number format. */}
        <div aria-hidden="true" className="relative h-40 w-10 shrink-0 sm:h-48">
          {ticks.map((tick) => (
            <span
              key={tick}
              className="text-muted-foreground absolute right-0 -translate-y-1/2 text-[11px] tabular-nums"
              style={{ top: `${y(tick)}%` }}
            >
              {formatNumber(tick, locale)}
            </span>
          ))}
        </div>

        <div
          role="img"
          tabIndex={0}
          aria-label={t("profile.ratingProgress.chartLabel", {
            speed: t(speedClassKey(speedClass)),
            games,
            from: formatNumber(vertices[0]?.rating ?? 0, locale),
            to: formatNumber(vertices[last]?.rating ?? 0, locale),
          })}
          onPointerMove={onPointerMove}
          onPointerLeave={() => {
            setActive(last);
          }}
          onKeyDown={onKeyDown}
          className="focus-visible:ring-ring relative h-40 min-w-0 flex-1 touch-pan-y rounded-sm focus-visible:ring-2 focus-visible:outline-none sm:h-48"
        >
          <svg
            aria-hidden="true"
            viewBox="0 0 100 100"
            preserveAspectRatio="none"
            className="absolute inset-0 size-full overflow-visible"
          >
            {ticks.map((tick) => (
              <line
                key={tick}
                x1={0}
                x2={100}
                y1={y(tick)}
                y2={y(tick)}
                className="stroke-border"
                strokeWidth={1}
                vectorEffect="non-scaling-stroke"
              />
            ))}
            <polyline
              points={line}
              fill="none"
              stroke={stroke}
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
          </svg>

          {/* HTML rather than SVG so a stretched viewBox cannot make an
              ellipse of a dot. The crosshair finds the game; the ring is
              the card's own surface, so the dot lifts off the line. */}
          <span
            aria-hidden="true"
            className="bg-border absolute inset-y-0 w-px"
            style={{ left: `${x(active)}%` }}
          />
          {[active, last].map((index, order) => (
            <span
              key={order}
              aria-hidden="true"
              className="ring-card absolute size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2"
              style={{
                left: `${x(index)}%`,
                top: `${y(vertices[index]?.rating ?? 0)}%`,
                background: stroke,
              }}
            />
          ))}
        </div>
      </div>

      <p className="text-muted-foreground flex justify-between gap-3 pl-12 text-xs">
        <span>
          {t(
            history.has_earlier
              ? "profile.ratingProgress.lastGames"
              : "profile.ratingProgress.allGames",
            { games },
          )}
        </span>
        <span className={cn("font-medium", accent.text)}>{t(speedClassKey(speedClass))}</span>
      </p>

      <details className="text-sm">
        <summary className="text-muted-foreground hover:text-foreground focus-visible:ring-ring w-fit cursor-pointer rounded-sm text-xs focus-visible:ring-2 focus-visible:outline-none">
          {t("profile.ratingProgress.showTable")}
        </summary>
        <div className="mt-2 max-h-64 overflow-y-auto">
          <table className="w-full text-left text-xs tabular-nums">
            <caption className="sr-only">{t("profile.ratingProgress.title")}</caption>
            <thead className="text-muted-foreground">
              <tr>
                <th scope="col" className="py-1 font-medium">
                  {t("profile.ratingProgress.colDate")}
                </th>
                <th scope="col" className="py-1 text-right font-medium">
                  {t("profile.ratingProgress.colRating")}
                </th>
                <th scope="col" className="py-1 text-right font-medium">
                  {t("profile.ratingProgress.colChange")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-border divide-y">
              {[...history.points].reverse().map((point) => (
                <tr key={point.match_id}>
                  <td className="py-1">{formatDate(point.at, locale)}</td>
                  <td className="py-1 text-right">{formatNumber(point.after, locale)}</td>
                  <td className="py-1 text-right">{formatRatingChange(point.delta, locale)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
