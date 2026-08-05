import { Link } from "@tanstack/react-router";

import type { Side } from "@/entities/board";
import type { GameState } from "@/features/game/model/state";
import { type ClockReading, formatClock } from "@/features/game/model/use-clock";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import type { ConnectionStatus } from "@/shared/realtime";
import { Button, Card, CardContent } from "@/shared/ui";

/**
 * Everything beside the board — A64-020.5B §19, §20, §21, §23.
 *
 * One file because these are one panel: two clocks, two names, a status
 * line and a result. Splitting them would be four components that only ever
 * appear together and each of which needs the same three props.
 */

/**
 * One side's clock.
 *
 * `tabular-nums` so the digits do not shift the layout as they change —
 * a clock that jitters horizontally is the most distracting thing that can
 * be next to a board.
 *
 * **Not announced.** §19 and §21 both forbid announcing every tick; there
 * is no live region here, and the accessible name carries the side so a
 * screen-reader user can read it on demand.
 */
function ClockFace({
  side,
  ms,
  active,
  awaiting,
}: {
  side: Side;
  ms: number;
  active: boolean;
  awaiting: boolean;
}) {
  const { t, locale } = useTranslation();
  const label = t(side === "light" ? "game.side.light" : "game.side.dark");

  return (
    <div
      className={cn(
        "border-border flex items-baseline justify-between rounded-md border px-3 py-2",
        active && "border-primary bg-primary/5",
      )}
    >
      <span className="text-muted-foreground text-xs">{label}</span>
      <span
        aria-label={t("game.clock.label", { side: label })}
        className="text-xl font-semibold tabular-nums"
      >
        {formatClock(ms, locale)}
      </span>
      {awaiting && active && <span className="sr-only">{t("game.clock.awaitingServer")}</span>}
    </div>
  );
}

/**
 * The status line.
 *
 * `role="status"` — a polite live region, so a turn change or a rejection
 * is announced when the reader pauses rather than interrupting. §21 asks
 * for textual labels rather than a board glow, and this is the text.
 */
function StatusLine({ state, connection }: { state: GameState; connection: ConnectionStatus }) {
  const { t } = useTranslation();

  const message = ((): TranslationKey => {
    if (connection === "offline") return "game.connection.offline";
    if (state.phase === "reconnecting") return "game.connection.reconnecting";
    if (state.phase === "resyncing") return "game.connection.resyncing";
    if (state.phase === "joining" || state.phase === "loading")
      return "game.connection.connecting";
    if (state.phase === "submitting_move") return "game.turn.pending";
    if (state.phase === "completed") return "game.result.title";
    return state.sideToMove === state.side ? "game.turn.yours" : "game.turn.opponent";
  })();

  return (
    <div className="flex flex-col gap-1">
      <p role="status" className="text-sm font-medium">
        {t(message)}
      </p>
      {state.lastRejection !== null && (
        <p role="alert" className="text-destructive text-sm">
          {t(rejectionKey(state.lastRejection))}
        </p>
      )}
    </div>
  );
}

/**
 * A gateway refusal as a sentence.
 *
 * Only codes `GatewayErrorCode` actually publishes — §24 forbids inventing
 * one, and a table with entries the server cannot send is a table nobody
 * can trust. Anything unmapped falls to `unknown` rather than rendering a
 * raw code at a player.
 */
function rejectionKey(code: string): TranslationKey {
  const known: Record<string, TranslationKey> = {
    not_your_turn: "game.errors.not_your_turn",
    illegal_move: "game.errors.illegal_move",
    stale_state: "game.errors.stale_state",
    clock_expired: "game.errors.clock_expired",
    match_not_active: "game.errors.match_not_active",
    not_a_participant: "game.errors.not_a_participant",
    room_unavailable: "game.errors.room_unavailable",
    not_in_room: "game.errors.not_in_room",
    rate_limited: "game.errors.rate_limited",
  };
  return known[code] ?? "game.errors.unknown";
}

/** The result, when the server says there is one. */
function Result({ state }: { state: GameState }) {
  const { t } = useTranslation();
  if (state.result === null) return null;

  const outcome =
    state.result.outcome === "draw"
      ? "game.result.draw"
      : state.result.winner === state.side
        ? "game.result.won"
        : "game.result.lost";

  const reasons: Record<string, TranslationKey> = {
    checkmate: "game.reason.checkmate",
    no_moves: "game.reason.checkmate",
    resignation: "game.reason.resignation",
    timeout: "game.reason.timeout",
    flag: "game.reason.timeout",
    agreement: "game.reason.agreement",
    adjudication: "game.reason.adjudication",
    abandonment: "game.reason.abandonment",
  };

  return (
    // `alert`, not `status`: the game ending is an interruption, and it is
    // the one moment on this screen that earns one.
    <div role="alert" className="border-border flex flex-col gap-3 rounded-md border p-4">
      <p className="text-lg font-semibold">{t(outcome)}</p>
      <p className="text-muted-foreground text-sm">
        {t("game.result.reason", {
          reason: t(reasons[state.result.termination_reason] ?? "game.reason.unknown"),
        })}
      </p>
      <Button asChild variant="outline" className="min-h-11 self-start">
        <Link to="/play">{t("game.result.backToLobby")}</Link>
      </Button>
    </div>
  );
}

export function GamePanel({
  state,
  clock,
  connection,
}: {
  state: GameState;
  clock: ClockReading;
  connection: ConnectionStatus;
}) {
  const { t } = useTranslation();
  // The viewer's own clock at the bottom, matching the board's orientation:
  // a player looks at one corner of the screen for their own time.
  const near = state.side ?? "light";
  const far = near === "light" ? "dark" : "light";

  const msOf = (side: Side) => (side === "light" ? clock.lightMs : clock.darkMs);

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 pt-6">
        <p className="text-muted-foreground text-xs">{t("game.players.opponent")}</p>
        <ClockFace
          side={far}
          ms={msOf(far)}
          active={clock.activeSide === far}
          awaiting={clock.awaitingServer}
        />

        <StatusLine state={state} connection={connection} />

        <ClockFace
          side={near}
          ms={msOf(near)}
          active={clock.activeSide === near}
          awaiting={clock.awaitingServer}
        />
        <p className="text-muted-foreground text-xs">{t("game.players.you")}</p>

        <Result state={state} />
      </CardContent>
    </Card>
  );
}
