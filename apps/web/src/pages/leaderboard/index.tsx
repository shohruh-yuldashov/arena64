import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useMemo, useRef } from "react";

import { countriesIn, countryFlag, countryName, toCountryCode } from "@/entities/country";
import { primaryRating } from "@/entities/profile";
import { isAuthenticated } from "@/entities/session";
import { speedClassKey } from "@/entities/time-control";
import { useSession } from "@/features/auth/model/session-provider";
import type {
  LeaderboardFilter,
  LeaderboardPlayer,
  LeaderboardStanding,
} from "@/features/leaderboard/api";
import {
  isLeaderboardSpeedClass,
  LEADERBOARD_PAGE_SIZE,
  LEADERBOARD_SPEED_CLASSES,
  useLeaderboard,
  useMyStanding,
} from "@/features/leaderboard/model/queries";
import { useMyRatings } from "@/features/profile/model/queries";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatNumber } from "@/shared/lib/format";
import { speedAccent } from "@/shared/lib/speed-accent";
import { Button, ListState } from "@/shared/ui";

/**
 * The leaderboard — A64-033.
 *
 * ## The server decides who is on it
 *
 * Every row arrives already eligible and already ranked: a deactivated or
 * restricted account, a frozen rating and a hidden country never reach
 * this page to be filtered. The page renders what arrived and numbers
 * nothing itself — `rank` is the server's.
 *
 * ## Filters live in the URL
 *
 * `?speed=` and `?country=` are the ladder, so a ladder is a link someone
 * can send, and Back walks between the ones visited. Choosing a country
 * here selects a ladder and nothing else: it never touches the player's own
 * country or the interface language.
 *
 * ## One table at every width
 *
 * A real `<table>`, because rank, player and rating are columns a reader
 * compares down the page. Below `sm` the games column folds under the
 * player's name instead of being drawn a second time as cards — one
 * structure, nothing rendered twice.
 *
 * ## A sparse country fills itself in
 *
 * The server examines a bounded number of ladder rows per request, so a
 * small country's first page can come back short — even empty — with a
 * cursor to continue. The page continues on its own a few times before
 * offering "Load more", so "nobody here" is only ever said when the ladder
 * is exhausted.
 */

/** Extra requests made unasked while a page is still short. */
const AUTO_CONTINUE = 4;

export default function LeaderboardPage() {
  const { t, locale } = useTranslation();
  const navigate = useNavigate({ from: "/leaderboard" });
  const raw = useSearch({ from: "/leaderboard" });
  // Checked again here, not only in the route's `validateSearch`: the root
  // route passes the raw query through and a child's output is merged over
  // it, so a value the validator dropped is still on `search` — the same
  // reason `next` is checked again at use by `safeRedirect`.
  const search = {
    speed: isLeaderboardSpeedClass(raw.speed) ? raw.speed : undefined,
    country: toCountryCode(typeof raw.country === "string" ? raw.country : null) ?? undefined,
  };
  const { state: session } = useSession();
  const viewerId = isAuthenticated(session) ? session.user.id : null;
  const myRatings = useMyRatings();

  // No class in the URL: open on the one the viewer plays most, as their
  // profile does, else the server's own default.
  //
  // Nothing is asked for until that is known — settled either way, so a
  // failed ratings read still lets the page fall back — because asking for
  // the default first would draw one ladder and swap it for another.
  const fallback = primaryRating(myRatings.data?.ratings ?? [])?.speed_class;
  const speedClass =
    search.speed ?? (isLeaderboardSpeedClass(fallback) ? fallback : "classical");
  const filter: LeaderboardFilter = { speedClass, country: search.country ?? null };
  const decided = search.speed !== undefined || !myRatings.isPending;

  const ladder = useLeaderboard(decided ? filter : null);
  const standing = useMyStanding(decided ? filter : null);
  const players = ladder.data?.pages.flatMap((page) => page.players) ?? [];

  // Bounded: at most `AUTO_CONTINUE` unasked requests per ladder chosen.
  const continued = useRef(0);
  useEffect(() => {
    continued.current = 0;
  }, [speedClass, filter.country]);
  useEffect(() => {
    if (
      players.length < LEADERBOARD_PAGE_SIZE &&
      ladder.hasNextPage &&
      !ladder.isFetching &&
      continued.current < AUTO_CONTINUE
    ) {
      continued.current += 1;
      void ladder.fetchNextPage();
    }
  }, [players.length, ladder]);

  const searching = players.length === 0 && ladder.hasNextPage;
  const countries = useMemo(() => countriesIn(locale), [locale]);
  const countryLabel = filter.country === null ? null : countryName(filter.country, locale);

  return (
    <section className="mx-auto flex w-full max-w-4xl flex-col gap-5 py-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">{t("leaderboard.title")}</h1>
        <p className="text-muted-foreground text-sm">{t("leaderboard.subtitle")}</p>
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <nav
          aria-label={t("leaderboard.speedClass")}
          className="bg-muted inline-flex w-fit rounded-lg p-0.5"
        >
          {LEADERBOARD_SPEED_CLASSES.map((speed) => (
            <Link
              key={speed}
              to="/leaderboard"
              search={(previous) => ({ ...previous, speed })}
              aria-current={speed === speedClass ? "page" : undefined}
              className={cn(
                "focus-visible:ring-ring inline-flex min-h-10 items-center rounded-md px-3 text-sm font-medium focus-visible:ring-2 focus-visible:outline-none",
                speed === speedClass
                  ? "bg-card text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {t(speedClassKey(speed))}
            </Link>
          ))}
        </nav>

        <label className="flex flex-col gap-1.5 text-sm font-medium">
          {t("leaderboard.country")}
          <select
            value={filter.country ?? ""}
            onChange={(event) => {
              const chosen = event.target.value;
              void navigate({
                search: (previous) => ({
                  ...previous,
                  country: chosen === "" ? undefined : chosen,
                }),
              });
            }}
            className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 dark:bg-input/30 h-10 min-w-0 rounded-md border px-3 text-sm font-normal outline-none focus-visible:ring-[3px] sm:w-64"
          >
            <option value="">{t("leaderboard.allCountries")}</option>
            {countries.map((country) => (
              <option key={country.code} value={country.code}>
                {`${country.flag} ${country.name}`}
              </option>
            ))}
          </select>
        </label>
      </div>

      <Standing
        standing={standing.data}
        speedLabel={t(speedClassKey(speedClass))}
        countryLabel={countryLabel}
      />

      <ListState
        isPending={ladder.isPending}
        isError={ladder.isError}
        isEmpty={players.length === 0 && !ladder.hasNextPage}
        loadingLabel={t("leaderboard.loading")}
        errorMessage={t("leaderboard.error")}
        emptyTitle={
          countryLabel === null
            ? t("leaderboard.empty")
            : t("leaderboard.emptyCountry", { country: countryLabel })
        }
        emptyHint={t("leaderboard.emptyHint")}
        pendingRows={6}
        pendingRowClassName="h-12"
        onRetry={() => void ladder.refetch()}
      >
        {players.length > 0 && (
          <div className="border-border bg-card overflow-hidden rounded-xl border">
            {/* `table-fixed` with a width on every column but the player's:
                an auto-sized table grows to its longest name, and inside
                this rounded card that pushed the rating column out of sight
                on a phone. Fixed, the player column takes what is left and
                a long name truncates instead. */}
            <table className="w-full table-fixed text-sm">
              <caption className="sr-only">
                {t("leaderboard.caption", {
                  speed: t(speedClassKey(speedClass)),
                  country: countryLabel ?? t("leaderboard.allCountries"),
                })}
              </caption>
              <thead className="bg-muted/50 text-muted-foreground text-xs">
                <tr>
                  <th
                    scope="col"
                    className="w-14 px-3 py-2 text-right font-medium sm:w-20 sm:px-4"
                  >
                    {t("leaderboard.rank")}
                  </th>
                  <th scope="col" className="px-3 py-2 text-left font-medium">
                    {t("leaderboard.player")}
                  </th>
                  <th
                    scope="col"
                    className="hidden w-24 px-3 py-2 text-right font-medium sm:table-cell"
                  >
                    {t("leaderboard.games")}
                  </th>
                  <th
                    scope="col"
                    className="w-24 px-3 py-2 text-right font-medium sm:w-28 sm:px-4"
                  >
                    {t("leaderboard.rating")}
                  </th>
                </tr>
              </thead>
              <tbody className="divide-border divide-y">
                {players.map((player) => (
                  <Row
                    key={player.player_id}
                    player={player}
                    isYou={player.player_id === viewerId}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {searching && (
          <p role="status" className="text-muted-foreground text-sm">
            {t("leaderboard.searching")}
          </p>
        )}

        {ladder.hasNextPage ? (
          <Button
            variant="outline"
            className="min-h-11 self-center"
            disabled={ladder.isFetchingNextPage}
            onClick={() => void ladder.fetchNextPage()}
          >
            {t(ladder.isFetchingNextPage ? "leaderboard.loadingMore" : "leaderboard.loadMore")}
          </Button>
        ) : (
          players.length > 0 && (
            <p role="status" className="text-muted-foreground py-2 text-center text-sm">
              {t("leaderboard.end")}
            </p>
          )
        )}
      </ListState>
    </section>
  );
}

function Row({ player, isYou }: { player: LeaderboardPlayer; isYou: boolean }) {
  const { t, locale } = useTranslation();
  const name = player.display_name ?? player.username;
  const code = toCountryCode(player.country);

  return (
    <tr className={cn(isYou && "bg-primary/5")}>
      <td className="px-3 py-2.5 text-right font-semibold tabular-nums sm:px-4">
        {formatNumber(player.rank, locale)}
      </td>
      <td className="min-w-0 px-3 py-2.5">
        <div className="flex min-w-0 flex-col">
          <span className="flex min-w-0 items-center gap-2">
            {code !== null && (
              // The flag is decoration; the country's name is in its title
              // and in the profile one click away.
              <span aria-hidden="true" title={countryName(code, locale)} className="shrink-0">
                {countryFlag(code)}
              </span>
            )}
            <Link
              to="/players/$username"
              params={{ username: player.username }}
              className="focus-visible:ring-ring truncate font-medium underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:outline-none"
            >
              {name}
            </Link>
            {isYou && (
              <span className="bg-primary/10 text-primary shrink-0 rounded px-1.5 text-xs font-medium">
                {t("leaderboard.you")}
              </span>
            )}
          </span>
          <span className="text-muted-foreground truncate text-xs">
            @{player.username}
            {code !== null && (
              <span className="sr-only">{`, ${countryName(code, locale)}`}</span>
            )}
            {/* The games column, folded in below `sm`. */}
            <span className="sm:hidden">
              {" · "}
              {t("leaderboard.gamesCount", { count: player.games_played })}
            </span>
          </span>
        </div>
      </td>
      <td className="text-muted-foreground hidden px-3 py-2.5 text-right tabular-nums sm:table-cell">
        {formatNumber(player.games_played, locale)}
      </td>
      <td className="px-3 py-2.5 text-right sm:px-4">
        <span className="font-semibold tabular-nums">
          {formatNumber(Math.round(player.rating), locale)}
        </span>
        {player.is_provisional && (
          <span className="text-muted-foreground block text-xs">
            {t("leaderboard.provisional")}
          </span>
        )}
      </td>
    </tr>
  );
}

/**
 * The viewer's own place, from the same filter as the table.
 *
 * Four answers, each said plainly: a rank; further down than ranks are
 * counted, with the rating; not on this particular ladder (usually a
 * country the profile does not show); or no rating in this class yet.
 */
function Standing({
  standing,
  speedLabel,
  countryLabel,
}: {
  standing: LeaderboardStanding | undefined;
  speedLabel: string;
  countryLabel: string | null;
}) {
  const { t, locale } = useTranslation();
  if (standing === undefined) return null;

  const rating =
    standing.rating === null || standing.rating === undefined
      ? null
      : formatNumber(Math.round(standing.rating), locale);
  const accent = speedAccent(standing.speed_class);

  const message = (() => {
    switch (standing.status) {
      case "ranked":
        return t("leaderboard.standing.ranked", {
          rank: formatNumber(standing.rank ?? 0, locale),
        });
      case "beyond":
        return t("leaderboard.standing.beyond");
      case "not_listed":
        return countryLabel === null
          ? t("leaderboard.standing.notListed")
          : t("leaderboard.standing.notListedCountry", { country: countryLabel });
      default:
        return t("leaderboard.standing.unrated", { speed: speedLabel });
    }
  })();

  return (
    <div
      role="status"
      className="border-border bg-card flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 rounded-xl border px-4 py-3"
    >
      <p className="text-sm">
        <span className="text-muted-foreground">{t("leaderboard.standing.title")}: </span>
        <span className="font-medium">{message}</span>
      </p>
      {rating !== null && (
        <p className="text-sm">
          <span className={cn("font-medium", accent.text)}>{speedLabel}</span>{" "}
          <span className="font-semibold tabular-nums">{rating}</span>
        </p>
      )}
      {standing.status === "unrated" && (
        <Link
          to="/play"
          className="text-primary focus-visible:ring-ring rounded-sm text-sm font-medium underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:outline-none"
        >
          {t("leaderboard.standing.play")}
        </Link>
      )}
    </div>
  );
}
