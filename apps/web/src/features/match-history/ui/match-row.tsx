import { Link } from "@tanstack/react-router";

import { formatMillis, speedClassKey } from "@/entities/time-control";
import type { MatchHistoryEntry } from "@/features/match-history/api";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { formatDateTime, formatRelativeTime } from "@/shared/lib/format";
import { speedAccent } from "@/shared/lib/speed-accent";
import { Avatar, AvatarFallback, AvatarImage } from "@/shared/ui";

/**
 * One finished match, as a list renders it — A64-020.5F §17, §23, §24.
 *
 * Every field comes from the entry. **Nothing is recomputed**: the result
 * is the server's outcome read against the viewer's seat, not derived from
 * a board; the opponent arrives composed, so this issues no request of its
 * own.
 *
 * ## The result is not a colour
 *
 * §23. A win, a loss and a draw are three words as well as three colours,
 * because a reader who cannot distinguish the colours must still be able to
 * read the row — and because a row that communicates only by colour tells a
 * screen reader nothing at all.
 */

/** Which side the viewer played, from the two seat ids. */
function seatOf(entry: MatchHistoryEntry, viewerId: string): "light" | "dark" | null {
  if (entry.light_player_id === viewerId) return "light";
  if (entry.dark_player_id === viewerId) return "dark";
  return null;
}

/**
 * The result from this viewer's seat.
 *
 * `null` for a match the viewer did not play — a rated game somebody else's
 * history surfaced — where "you won" would be a lie and the outcome is
 * still worth showing as light's or dark's.
 */
function resultKey(entry: MatchHistoryEntry, seat: "light" | "dark" | null): TranslationKey {
  if (entry.outcome === "draw") return "history.result.draw";
  if (entry.outcome !== "win" || entry.winner === null) return "history.result.unfinished";
  if (seat === null) {
    return entry.winner === "light" ? "history.result.lightWon" : "history.result.darkWon";
  }
  return entry.winner === seat ? "history.result.won" : "history.result.lost";
}

/**
 * A termination reason as a sentence.
 *
 * The **server's** `TerminationReason` values, cross-checked against
 * `app/modules/game/domain/result.py` — the same table the replay summary
 * uses, and for the reason A64-020.5B's mistake taught: three invented
 * names meant an agreed draw read as "Unknown" for two phases.
 */
function reasonKey(reason: string | null): TranslationKey {
  const known: Record<string, TranslationKey> = {
    no_legal_moves: "game.reason.no_legal_moves",
    all_pieces_captured: "game.reason.all_pieces_captured",
    resignation: "game.reason.resignation",
    abort: "game.reason.abort",
    agreed_draw: "game.reason.agreed_draw",
    repetition: "game.reason.repetition",
    move_limit: "game.reason.move_limit",
    flag: "game.reason.timeout",
    flag_insufficient_material: "game.reason.timeout",
    abandonment: "game.reason.abandonment",
    adjudication: "game.reason.adjudication",
  };

  return (reason !== null ? known[reason] : undefined) ?? "game.reason.unknown";
}

export function MatchRow({ entry, viewerId }: { entry: MatchHistoryEntry; viewerId: string }) {
  const { t, locale } = useTranslation();

  const seat = seatOf(entry, viewerId);
  const result = resultKey(entry, seat);
  const won = result === "history.result.won";
  const lost = result === "history.result.lost";

  const clock = formatMillis(
    entry.time_control?.initial_ms ?? null,
    entry.time_control?.increment_ms ?? null,
    locale,
  );
  // Nullable *and* optional on the wire; the two absent cases are one
  // thing to a reader, so they collapse before anything renders.
  const speedClass = entry.speed_class ?? null;
  const played = entry.ended_at ?? entry.started_at;
  const name =
    entry.opponent?.display_name ?? entry.opponent?.username ?? t("history.unknownOpponent");

  const accent = speedClass === null ? null : speedAccent(speedClass);

  return (
    // One row, one link, one tab stop — A64-025.5C §23.
    //
    // The row carried a separate "View replay" at its end, so a list of
    // twenty matches was forty stops and the row itself was inert: a player
    // clicked the match they wanted and nothing happened. `after:inset-0`
    // makes the whole row the target while leaving exactly one anchor in
    // the accessibility tree, which is the same construction the home
    // page's destination cards use.
    <li className="relative">
      <div className="flex items-center gap-3 px-4 py-3 transition-colors duration-fast hover:bg-muted/50 sm:gap-4 sm:px-5">
        {/* The result leads. It is what somebody scans a history for, and
            it was the fourth thing on the row behind an avatar, a name and
            a clock. A fixed width so the outcomes line up down the edge —
            the same chip the tournament history uses. */}
        <span
          className={cn(
            // `min-w`, not `w` — A64-025.5D. A fixed 4rem chip fitted
            // "Won", "Lost" and "Draw" and clipped "Yutqazdingiz",
            // "Поражение" and "Без результата": the width was chosen against
            // English and nothing else. Short labels still line up; a long
            // one grows rather than being cut, which is the trade a
            // multilingual product has to make in that direction.
            //
            // A grid with `display: contents` would align every locale
            // perfectly and is not worth it: it has a history of stripping
            // list semantics in screen readers, and the list semantics here
            // were argued for on purpose.
            "inline-flex h-8 min-w-16 shrink-0 items-center justify-center rounded-md px-2 text-xs font-semibold",
            // `--success`, not `--primary`. A64-025.9 §18.7 gives the brand
            // hue one job — *interaction* — and a finished result is not
            // one; the profile's own win/loss bar has been success-red-grey
            // since that phase and this row disagreed with it.
            won && "bg-success/15 text-success",
            lost && "bg-destructive/15 text-destructive",
            !won && !lost && "bg-muted text-muted-foreground",
          )}
        >
          {t(result)}
        </span>

        {/* Dense rows use the thumbnail, never a full avatar read — §17. */}
        <Avatar className="hidden size-9 shrink-0 sm:flex">
          {entry.opponent?.avatar_thumbnail_url != null && (
            <AvatarImage src={entry.opponent.avatar_thumbnail_url} alt="" />
          )}
          <AvatarFallback aria-hidden="true">{name.slice(0, 2).toUpperCase()}</AvatarFallback>
        </Avatar>

        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <Link
            to="/games/$matchId/replay"
            params={{ matchId: entry.match_id }}
            // An unambiguous accessible name — §23. "Replay" repeated down
            // a list gives a screen reader twenty identical links; naming
            // the opponent makes each one distinguishable.
            aria-label={t("history.replayOf", { opponent: name })}
            className="focus-visible:ring-ring truncate text-sm font-medium after:absolute after:inset-0 focus-visible:ring-2 focus-visible:outline-none"
          >
            {t("history.versus", { opponent: name })}
          </Link>
          <span className="text-muted-foreground truncate text-xs">
            {t(entry.rated ? "play.mode.ranked" : "play.mode.casual")}
            {clock !== null && ` · ${clock}`}
            {/* A64-025.9 §18.7. This printed the **raw enum** — `blitz`, in
                every locale — and now reads as the translated name in the
                class's own colour, the same one the profile's cards use. */}
            {speedClass !== null && accent !== null && (
              <>
                {" · "}
                <span className={cn("font-medium", accent.text)}>
                  {t(speedClassKey(speedClass))}
                </span>
              </>
            )}
            {" · "}
            {t(reasonKey(entry.termination_reason))}
          </span>
        </div>

        {/* Relative, with the instant on the element — A64-025.10 §21. A
            history is scanned for "when", and `9/4/26` is a date somebody
            has to decode. */}
        <time
          dateTime={played}
          title={formatDateTime(played, locale) ?? ""}
          className="text-muted-foreground shrink-0 text-xs"
        >
          {formatRelativeTime(played, locale, t)}
        </time>
      </div>
    </li>
  );
}
