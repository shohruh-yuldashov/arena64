import { Link } from "@tanstack/react-router";

import type { GameState } from "@/features/game/model/state";
import { useRatingResult } from "@/features/game/model/use-rating-result";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import type { ConnectionStatus } from "@/shared/realtime";
import { Button } from "@/shared/ui";

/**
 * Everything beside the board — A64-020.5B §19, §20, §21, §23.
 *
 * One file because these are one panel: two clocks, two names, a status
 * line and a result. Splitting them would be four components that only ever
 * appear together and each of which needs the same three props.
 */

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

  // The tone is the outcome, from this seat. A spectator has no seat, so
  // `side` is `null` and every decisive result reads as a loss — which is
  // why the neutral tone is used when there is nobody to have won.
  const outcomeTone: "win" | "loss" | "draw" =
    state.result.outcome === "draw" || state.side === null
      ? "draw"
      : state.result.winner === state.side
        ? "win"
        : "loss";

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
    // A64-025.6A §20. The result was a bordered box with a bold line in it.
    // It is the moment the game is *about*, so it now leads with the outcome
    // set large on a tinted surface whose tone is the outcome — win on
    // `--success`, loss on `--destructive`, draw on the neutral muted — with
    // the reason under it and the rating consequence under that. §20's
    // hierarchy, in that order.
    //
    // It stays beside the board rather than covering it: a player wants to
    // see the final position, and a modal over it is the one thing §19
    // forbids.
    <div
      role="alert"
      className={cn(
        "flex flex-col gap-3 rounded-lg border p-4",
        outcomeTone === "win" && "border-success/40 bg-success/10",
        outcomeTone === "loss" && "border-destructive/40 bg-destructive/10",
        outcomeTone === "draw" && "border-border bg-muted/50",
      )}
    >
      <div className="flex flex-col gap-1">
        <p className="text-2xl leading-tight font-semibold tracking-tight">{t(outcome)}</p>
        <p className="text-muted-foreground text-sm">
          {t("game.result.reason", {
            reason: t(reasons[state.result.termination_reason] ?? "game.reason.unknown"),
          })}
        </p>
      </div>

      <RatingResultBlock matchId={state.matchId} viewerId={viewerId} />

      <div className="flex flex-col gap-2">
        {/* A64-020.5E §22. The one existing surface with a real match id —
            there is no match-history UI yet, so this is where a replay is
            reachable from. The board this panel sits beside is already the
            finished position; the link is for looking at how it got there. */}
        {/* The primary next action is another game — that is what a player
            who just finished one wants. The replay is secondary and the
            board beside it is already the final position. */}
        <Button asChild className="w-full">
          <Link to="/play">{t("game.result.backToLobby")}</Link>
        </Button>
        <Button asChild variant="outline" className="w-full">
          <Link to="/games/$matchId/replay" params={{ matchId: state.matchId }}>
            {t("replay.openReplay")}
          </Link>
        </Button>
      </div>
    </div>
  );
}

/**
 * What the game is doing, and how it ended.
 *
 * A64-025.6 moved the clocks and the seats out of here and onto the board,
 * where a player can read them without looking away — see `PlayerSeat`. The
 * transient quick messages went with their seats for the same reason. What
 * is left is the pair of things that are *about* the match rather than about
 * a player: the status line and, once there is one, the result.
 */
export function GamePanel({
  state,
  connection,
  viewerId,
}: {
  state: GameState;
  connection: ConnectionStatus;
  /** Whose rating the result block reports. `null` for a spectator. */
  viewerId: string | null;
}) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-3">
      {/* What is at stake — A64-025.6D §28. The room named the two players,
          their clocks and their ratings, and never said whether the result
          counted: the one fact a player weighs before spending twenty
          minutes. `rated` has been on the snapshot since the room was
          built. Nothing is inferred — `null` before the first snapshot
          renders nothing at all. */}
      {state.rated !== null && (
        <p className="text-muted-foreground text-xs font-medium">
          {t(state.rated ? "play.mode.ranked" : "play.mode.casual")}
        </p>
      )}

      <StatusLine state={state} connection={connection} />
      <Result state={state} viewerId={viewerId} />
    </div>
  );
}
