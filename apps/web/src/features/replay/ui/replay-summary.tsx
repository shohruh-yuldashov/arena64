import { formatMillis } from "@/entities/time-control";
import type { MatchReplay, ReplaySeat } from "@/features/replay/api";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import type { Side } from "@/shared/realtime";
import { Card, CardContent } from "@/shared/ui";

/**
 * Who played, under what, and how it ended — A64-020.5E §13, §14, §15.
 *
 * Every field comes from the replay response. **Nothing is inferred**:
 * §14 forbids deriving `rated` from the time control or the route, and the
 * backend now publishes it precisely so that nobody has to.
 *
 * The participants arrive composed — one batched lookup on the server —
 * so this issues no profile request of its own (§23).
 */

/**
 * A termination reason as a sentence.
 *
 * The **server's** `TerminationReason` values, cross-checked against
 * `app/modules/game/domain/result.py`. §15 names the trap explicitly and
 * this codebase already fell into it once: A64-020.5B invented
 * `checkmate`, `no_moves` and `agreement`, none of which the gateway
 * sends, so an agreed draw rendered as "Unknown" for two phases.
 *
 * An unmapped value falls back to a translated sentence rather than a raw
 * identifier — a reason added later should read as unfamiliar, not as a
 * broken screen.
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

function Seat({ seat, side, isWinner }: { seat: ReplaySeat; side: Side; isWinner: boolean }) {
  const { t, locale } = useTranslation();
  const name = seat.display_name ?? seat.username ?? t("replay.seat.unknownPlayer");
  const rating =
    seat.rating_value !== null && seat.rating_value !== undefined
      ? new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(seat.rating_value)
      : null;

  return (
    <div className="flex items-baseline justify-between gap-2">
      <div className="flex min-w-0 flex-col">
        <span className="truncate text-sm font-medium">
          {name}
          {/* Not colour alone — §20. The winner is named in text. */}
          {isWinner && (
            <span className="text-muted-foreground ml-2 text-xs font-normal">
              {t("replay.seat.winner")}
            </span>
          )}
        </span>
        <span className="text-muted-foreground text-xs">
          {t(side === "light" ? "game.side.light" : "game.side.dark")}
          {seat.username !== null && seat.username !== undefined && ` · @${seat.username}`}
        </span>
      </div>
      {rating !== null && (
        <span className="text-sm tabular-nums">
          {rating}
          {seat.is_provisional === true && (
            <span className="text-muted-foreground" aria-hidden="true">
              ?
            </span>
          )}
        </span>
      )}
    </div>
  );
}

export function ReplaySummary({ replay }: { replay: MatchReplay }) {
  const { t, locale } = useTranslation();

  const clock = formatMillis(
    replay.time_control?.initial_ms ?? null,
    replay.time_control?.increment_ms ?? null,
    locale,
  );
  const ended = replay.ended_at ?? replay.created_at;

  const outcome: TranslationKey =
    replay.outcome === "draw"
      ? "replay.result.draw"
      : replay.winner === "light"
        ? "replay.result.lightWon"
        : replay.winner === "dark"
          ? "replay.result.darkWon"
          : "replay.result.unfinished";

  return (
    <Card>
      <CardContent className="flex flex-col gap-4 pt-6">
        <div className="flex flex-col gap-2">
          <Seat seat={replay.light} side="light" isWinner={replay.winner === "light"} />
          <Seat seat={replay.dark} side="dark" isWinner={replay.winner === "dark"} />
        </div>

        {/* `alert`: the result is the thing a reader came for, and it is
            the one part of this panel worth interrupting for. */}
        <div role="alert" className="border-border rounded-md border p-3">
          <p className="font-semibold">{t(outcome)}</p>
          <p className="text-muted-foreground text-sm">
            {t("game.result.reason", { reason: t(reasonKey(replay.termination_reason)) })}
          </p>
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted-foreground">{t("play.form.mode")}</dt>
          <dd className="font-medium">
            {t(replay.rated ? "play.mode.ranked" : "play.mode.casual")}
          </dd>

          <dt className="text-muted-foreground">{t("play.form.timeControl")}</dt>
          <dd className="font-medium tabular-nums">{clock ?? t("replay.meta.untimed")}</dd>

          {replay.speed_class !== null && (
            <>
              <dt className="text-muted-foreground">{t("play.waiting.speed")}</dt>
              <dd className="font-medium">{replay.speed_class}</dd>
            </>
          )}

          <dt className="text-muted-foreground">{t("replay.meta.played")}</dt>
          <dd className="font-medium">
            {/* The instant is the server's; the text is the reader's locale. */}
            <time dateTime={ended}>
              {new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(new Date(ended))}
            </time>
          </dd>

          <dt className="text-muted-foreground">{t("replay.meta.moves")}</dt>
          <dd className="font-medium tabular-nums">
            {new Intl.NumberFormat(locale).format(replay.plies.length)}
          </dd>

          <dt className="text-muted-foreground">{t("replay.meta.engine")}</dt>
          <dd className="font-medium tabular-nums">{replay.engine_version}</dd>
        </dl>
      </CardContent>
    </Card>
  );
}
