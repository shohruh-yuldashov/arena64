import { Link } from "@tanstack/react-router";

import { formatMillis } from "@/entities/time-control";
import type { MatchHistoryEntry } from "@/features/match-history/api";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
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
  const played = entry.ended_at ?? entry.started_at;
  const name =
    entry.opponent?.display_name ?? entry.opponent?.username ?? t("history.unknownOpponent");

  return (
    <li className="border-border flex flex-col gap-3 border-b py-3 last:border-b-0 sm:flex-row sm:items-center sm:gap-4">
      {/* Dense rows use the thumbnail, never a full avatar read — §17. */}
      <Avatar className="size-9 shrink-0">
        {entry.opponent?.avatar_thumbnail_url != null && (
          <AvatarImage src={entry.opponent.avatar_thumbnail_url} alt="" />
        )}
        <AvatarFallback aria-hidden="true">{name.slice(0, 2).toUpperCase()}</AvatarFallback>
      </Avatar>

      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-sm font-medium">
          {t("history.versus", { opponent: name })}
        </span>
        <span className="text-muted-foreground truncate text-xs">
          {t(entry.rated ? "play.mode.ranked" : "play.mode.casual")}
          {clock !== null && ` · ${clock}`}
          {entry.speed_class !== null && ` · ${entry.speed_class}`}
        </span>
      </div>

      <div className="flex flex-col sm:items-end">
        {/* The word carries the meaning; the colour only reinforces it. */}
        <span
          className={cn(
            "text-sm font-semibold",
            won && "text-primary",
            lost && "text-destructive",
          )}
        >
          {t(result)}
        </span>
        <span className="text-muted-foreground text-xs">
          {t(reasonKey(entry.termination_reason))}
        </span>
      </div>

      <div className="flex items-center gap-3 sm:w-40 sm:justify-end">
        <time dateTime={played} className="text-muted-foreground text-xs tabular-nums">
          {new Intl.DateTimeFormat(locale, { dateStyle: "short" }).format(new Date(played))}
        </time>
        {/* An unambiguous accessible name — §23. "Replay" repeated down a
            list gives a screen reader twenty identical links; naming the
            opponent makes each one distinguishable. */}
        <Link
          to="/games/$matchId/replay"
          params={{ matchId: entry.match_id }}
          aria-label={t("history.replayOf", { opponent: name })}
          className="text-primary min-h-11 min-w-11 self-center px-2 py-2 text-sm underline-offset-4 hover:underline focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none"
        >
          {t("replay.openReplay")}
        </Link>
      </div>
    </li>
  );
}
