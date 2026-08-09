import type { PlayerIdentity } from "@/features/game/model/use-player-identity";
import { useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import type { Side } from "@/shared/realtime";
import { Avatar, AvatarFallback, AvatarImage } from "@/shared/ui";

/** Seconds at which the remaining time becomes the loudest thing on screen. */
export const LOW_TIME_SECONDS = 10;

function formatClock(ms: number, locale: string): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${new Intl.NumberFormat(locale).format(minutes)}:${String(seconds).padStart(2, "0")}`;
}

/**
 * One player and their clock, attached to the board — A64-025.6 §5, §6, §7.
 *
 * ## Why identity and time are one component
 *
 * They answer one question together. "Whose move is it, and how long have
 * they got" was previously two facts in two places: a `ClockFace` in a side
 * panel and no identity at all beyond "Opponent" and "You". A player had to
 * look away from the board to find either.
 *
 * This sits directly above and below the board at every width, which is the
 * decision OQ-3 was waiting for. On a phone the clock is never in a side
 * panel because there is no side panel; on a desktop it is still adjacent
 * to the board rather than across the page.
 *
 * ## Active is three signals, never one colour
 *
 * The seat takes a brand border and a tint, the clock's digits go
 * `text-primary`, **and** the turn is stated in words underneath. WCAG
 * 1.4.1: somebody who cannot tell the two tints apart reads the sentence.
 *
 * ## Low time — a presentation rule, and only that
 *
 * Under ten seconds the clock takes `--warning` and gains a word. Ten
 * because it is what this product already uses: A64-025.5 tinted the match
 * offer's countdown at the same threshold, and two different definitions of
 * "nearly out of time" in one product is worse than an imperfect one.
 *
 * It is **presentation**, not a rule: nothing about the game changes, and
 * the server is still the only thing that flags a clock. A threshold
 * relative to the control — the last tenth of the base time — would be
 * better and is not possible today, because `ClockPayload` carries only the
 * remaining milliseconds and the snapshot does not carry the time control.
 * That gap is recorded in `specs/product-experience.md` §13.
 */
export function PlayerSeat({
  side,
  identity,
  ms,
  active,
  awaiting,
  isViewer,
  running,
}: {
  side: Side;
  /** `undefined` while the read is in flight, or if it failed. */
  identity: PlayerIdentity | undefined;
  ms: number;
  active: boolean;
  awaiting: boolean;
  isViewer: boolean;
  /** Whether the game is live. A finished clock is frozen, not urgent. */
  running: boolean;
}) {
  const { t, locale } = useTranslation();

  const sideLabel = t(side === "light" ? "game.side.light" : "game.side.dark");
  const name = identity?.display_name ?? identity?.username ?? t("game.players.unknown");
  const low = running && active && ms <= LOW_TIME_SECONDS * 1000;

  return (
    <div
      className={cn(
        "flex items-center gap-3 rounded-xl border px-3 py-2.5 transition-colors",
        // The active seat is the brand; the idle one recedes into the page
        // rather than competing with the board it frames.
        active ? "border-primary bg-primary/10 shadow-sm" : "border-border bg-muted/30",
      )}
    >
      <Avatar className="size-9 shrink-0">
        {identity?.thumbnail_url != null && <AvatarImage src={identity.thumbnail_url} alt="" />}
        <AvatarFallback aria-hidden="true">{name.slice(0, 2).toUpperCase()}</AvatarFallback>
      </Avatar>

      <div className="flex min-w-0 flex-col">
        <span className="truncate text-sm font-medium">{name}</span>
        <span className="text-muted-foreground truncate text-xs">
          {/* The seat's colour, and whose seat it is. Both matter: a player
              reconnecting needs to know which side they are. */}
          {isViewer ? `${sideLabel} · ${t("game.players.you")}` : sideLabel}
        </span>
      </div>

      {/* The clock has its own container, so it reads as an instrument
          rather than as one more line of text in the row. Fixed minimum
          width, so `9:59` and `10:00` do not move the name beside them. */}
      <div
        className={cn(
          "ml-auto flex min-w-[5.5rem] flex-col items-end rounded-lg border px-2.5 py-1",
          low
            ? "border-warning/50 bg-warning/10"
            : active
              ? "border-primary/40 bg-primary/5"
              : "border-transparent",
        )}
      >
        <span
          // The digits are not a live region: they change four times a
          // second and announcing that is unusable. The accessible name
          // carries the side so a reader can ask for it on demand.
          aria-label={t("game.clock.label", { side: sideLabel })}
          className={cn(
            "text-2xl leading-none font-semibold tabular-nums",
            low ? "text-warning" : active ? "text-primary" : "text-foreground",
          )}
        >
          {formatClock(ms, locale)}
        </span>
        {/* The word that makes the tint redundant. */}
        <span className="text-xs">
          {low ? (
            <span className="text-warning font-medium">{t("game.clock.low")}</span>
          ) : (
            <span className="text-muted-foreground">
              {active ? t("game.clock.running") : " "}
            </span>
          )}
        </span>
        {awaiting && active && (
          <span className="sr-only">{t("game.clock.awaitingServer")}</span>
        )}
      </div>
    </div>
  );
}
