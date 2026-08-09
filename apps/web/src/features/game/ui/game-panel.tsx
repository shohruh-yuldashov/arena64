import { Link } from "@tanstack/react-router";

import type { Side } from "@/entities/board";
import type { GameState } from "@/features/game/model/state";
import { type ClockReading, formatClock } from "@/features/game/model/use-clock";
import type { VisibleQuickMessage } from "@/features/game/model/use-quick-messages";
import { useRatingResult } from "@/features/game/model/use-rating-result";
import { QuickMessageBubble } from "@/features/game/ui/quick-message-bubble";
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

/**
 * What the match did to this player's rating — A64-023 §7, §8.
 *
 * Three states, and none of them is a fabricated zero:
 *
 *     rated + ready     1524 → 1537, +13
 *     rated + waiting   "Rating is being updated…"
 *     rated + late      "Rating update is taking longer than expected."
 *     casual            nothing at all
 *
 * The delta is the server's — see `rating.public.RatingChange.delta` on why
 * this platform subtracts in one place. Nothing here computes Glicko-2, and
 * nothing here reads the profile's current rating: a profile shows what a
 * player rates *now*, which after two quick games is not what this match
 * did.
 */
function RatingResultBlock({
  matchId,
  viewerId,
}: {
  matchId: string;
  viewerId: string | null;
}) {
  const { t, locale } = useTranslation();
  const rating = useRatingResult({ matchId, viewerId, enabled: true });

  // A casual match, or an answer that has not arrived at all yet. Rendering
  // a placeholder for the second would put "updating…" under every casual
  // result for one tick.
  if (rating.rated !== true) return null;

  const number = (value: number) => new Intl.NumberFormat(locale).format(value);

  return (
    <div className="flex flex-col gap-1">
      <p className="text-muted-foreground text-xs">{t("game.rating.title")}</p>
      {rating.change !== null ? (
        <p className="text-sm tabular-nums">
          <span>{number(rating.change.before)}</span>
          <span aria-hidden="true"> → </span>
          <span className="sr-only">{t("game.rating.becomes")}</span>
          <span className="font-medium">{number(rating.change.after)}</span>{" "}
          <span className={rating.change.delta < 0 ? "text-destructive" : "text-success"}>
            {/* The sign is part of the number, formatted rather than
                concatenated: a minus typed as a hyphen is the wrong glyph
                in every locale this product ships. */}
            {new Intl.NumberFormat(locale, { signDisplay: "exceptZero" }).format(
              rating.change.delta,
            )}
          </span>
        </p>
      ) : (
        <p role="status" className="text-muted-foreground text-sm">
          {t(rating.hasGivenUp ? "game.rating.late" : "game.rating.updating")}
        </p>
      )}
    </div>
  );
}

/** The result, when the server says there is one. */
function Result({ state, viewerId }: { state: GameState; viewerId: string | null }) {
  const { t } = useTranslation();
  if (state.result === null) return null;

  const outcome =
    state.result.outcome === "draw"
      ? "game.result.draw"
      : state.result.winner === state.side
        ? "game.result.won"
        : "game.result.lost";

  // The **server's** `TerminationReason` values, verbatim — A64-020.5C
  // §15. A64-020.5B guessed at three of these (`checkmate`, `no_moves`,
  // `agreement`) and the gateway sends none of them, so an agreed draw
  // rendered as "Unknown" and a win by capture did too. Cross-checked
  // against `app/modules/game/domain/result.py`.
  const reasons: Record<string, TranslationKey> = {
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
      <RatingResultBlock matchId={state.matchId} viewerId={viewerId} />

      <div className="flex flex-wrap gap-2">
        {/* A64-020.5E §22. The one existing surface with a real match id —
            there is no match-history UI yet, so this is where a replay is
            reachable from. The board this panel sits beside is already the
            finished position; the link is for looking at how it got there. */}
        <Button asChild variant="outline" className="min-h-11">
          <Link to="/games/$matchId/replay" params={{ matchId: state.matchId }}>
            {t("replay.openReplay")}
          </Link>
        </Button>
        <Button asChild variant="outline" className="min-h-11">
          <Link to="/play">{t("game.result.backToLobby")}</Link>
        </Button>
      </div>
    </div>
  );
}

export function GamePanel({
  state,
  clock,
  connection,
  viewerId,
  quickMessages,
}: {
  state: GameState;
  clock: ClockReading;
  connection: ConnectionStatus;
  /** Whose rating the result block reports. `null` for a spectator. */
  viewerId: string | null;
  /**
   * At most one transient message per seat — A64-023.2 §6.
   *
   * Passed in rather than subscribed to here, so this component stays a
   * pure rendering of what it is given and the panel has no socket of its
   * own. Defaulted, so every existing call site and test is unchanged.
   */
  quickMessages?: ReadonlyMap<Side, VisibleQuickMessage>;
}) {
  const { t } = useTranslation();
  // The viewer's own clock at the bottom, matching the board's orientation:
  // a player looks at one corner of the screen for their own time.
  const near = state.side ?? "light";
  const far = near === "light" ? "dark" : "light";

  const msOf = (side: Side) => (side === "light" ? clock.lightMs : clock.darkMs);

  // Adjacent to the seat that sent it, which is the whole of §6's
  // positioning rule — the panel already orders the two seats, so there is
  // no overlay and nothing can cover a board square or a clock.
  const bubbleOf = (side: Side) => quickMessages?.get(side);
  const farBubble = bubbleOf(far);
  const nearBubble = bubbleOf(near);

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 pt-6">
        <p className="text-muted-foreground text-xs">{t("game.players.opponent")}</p>
        {farBubble !== undefined && (
          // Keyed, so a replacing message remounts the live region and is
          // announced even when it repeats the previous one — §7.
          <QuickMessageBubble key={farBubble.key} bubble={farBubble} align="far" />
        )}
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
        {nearBubble !== undefined && (
          <QuickMessageBubble key={nearBubble.key} bubble={nearBubble} align="near" />
        )}
        <p className="text-muted-foreground text-xs">{t("game.players.you")}</p>

        <Result state={state} viewerId={viewerId} />
      </CardContent>
    </Card>
  );
}
