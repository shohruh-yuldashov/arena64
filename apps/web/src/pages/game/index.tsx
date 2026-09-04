import { Link, useParams } from "@tanstack/react-router";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { canInteract } from "@/features/game/model/state";
import { useClock } from "@/features/game/model/use-clock";
import { useGameRoom } from "@/features/game/model/use-game-room";
import { useMoveConfirmation } from "@/features/game/model/use-move-confirmation";
import { useMoveSelection } from "@/features/game/model/use-move-selection";
import { usePlayerIdentities } from "@/features/game/model/use-player-identity";
import { useQuickMessages } from "@/features/game/model/use-quick-messages";
import { GameBoard } from "@/features/game/ui/board";
import { GameControls } from "@/features/game/ui/game-controls";
import { GamePanel } from "@/features/game/ui/game-panel";
import { PendingMove } from "@/features/game/ui/pending-move";
import { PlayerSeat } from "@/features/game/ui/player-seat";
import { QuickMessageBubble } from "@/features/game/ui/quick-message-bubble";
import { QuickMessagePicker } from "@/features/game/ui/quick-message-picker";
import { usePreferences } from "@/features/profile/model/queries";
import { useTranslation } from "@/shared/i18n";
import { useHoldAppUpdate } from "@/shared/pwa";
import { useConnectionStatus } from "@/shared/realtime";
import { Button, Skeleton } from "@/shared/ui";

/**
 * One live game — A64-020.5B §2, §27, §30.
 *
 * The page owns the layout and nothing else: `useGameRoom` holds the
 * protocol, `useMoveSelection` holds the interaction, `useClock` holds the
 * countdown, and this arranges the three.
 *
 * ## Layout
 *
 * Board first in the DOM, always — it is the content, and on a phone it is
 * what should be under the thumb. The panel follows below on a narrow
 * screen and moves beside on a wide one, which is one `lg:` breakpoint
 * rather than a media-query cascade.
 *
 * The board's column is capped so a desktop does not render a
 * thousand-pixel board; the panel takes what is left.
 *
 * ## Leaving is handled by the hook
 *
 * `room.leave` and the socket's survival are `useGameRoom`'s cleanup (§30).
 * This page does nothing on unmount, which is what makes it impossible for
 * it to resign, decline or end a match by navigating away.
 *
 * ## Controls — A64-020.5C
 *
 * `GameControls` is mounted here, beside the panel, and given the same
 * `state` everything else renders from. It sends nothing itself: `command`
 * is `useGameRoom`'s, so every participant command goes through the one
 * socket, the one request registry and the one reducer.
 */
export default function GamePage() {
  const { t } = useTranslation();
  const { state: session } = useSession();
  const { matchId } = useParams({ from: "/games/$matchId" });
  const connection = useConnectionStatus();
  const { state, submit, command } = useGameRoom(matchId);
  const selection = useMoveSelection(state);

  // A64-025.14 §38. The last of the five gameplay preferences that nothing
  // read. `usePreferences` is an account read, so a spectator's query
  // simply fails and `data` stays undefined — which falls back to `false`,
  // the API's own default, and is the right answer for somebody who is not
  // playing.
  const preferences = usePreferences();
  const confirmation = useMoveConfirmation({
    enabled: preferences.data?.gameplay.confirm_move ?? false,
    sequence: state.sequence,
    onSubmit: (path) => void submit([...path]),
  });

  // The countdown runs only while a game is genuinely in progress. A
  // completed game's clock is frozen at whatever the last frame said, which
  // is the honest reading — §19's "paused/completed handling".
  const clock = useClock(
    state.clock,
    state.phase === "active" || state.phase === "submitting_move",
  );

  const interactive = canInteract(state);

  // A64-025.6 §5. The two seats, oriented the way the board is: the viewer
  // is always the near side. A spectator has no seat, so `light` is the
  // fallback and the labels below say which colour each is rather than
  // claiming one of them is "you".
  const near = state.side ?? "light";
  const far = near === "light" ? "dark" : "light";
  const identities = usePlayerIdentities([
    state.participants?.light ?? null,
    state.participants?.dark ?? null,
  ]);
  const running = state.phase === "active" || state.phase === "submitting_move";

  // A64-023.2. Its own hook and its own state, deliberately kept out of the
  // game reducer (§17): a quick message changes no board, no clock and no
  // ply, and a render failure here must not be able to reach one.
  //
  // `playable` is the same predicate the controls use — a terminal match
  // stops accepting messages (§10), and the server refuses them regardless.
  const quickMessages = useQuickMessages({
    matchId,
    viewerSide: state.side,
    playable:
      (state.phase === "active" || state.phase === "submitting_move") && state.result === null,
  });

  const farBubble = quickMessages.visible.get(far);
  const nearBubble = quickMessages.visible.get(near);

  // A64-020.9 §14. A service-worker activation reloads the page, and a
  // reload here is not a refresh — it is a clock still running while the
  // board is gone, and a resign or draw command whose answer nobody sees.
  // So this screen holds the update until the game is over.
  //
  // `reconnecting` and `resyncing` count: the game is still live, the
  // socket is merely between frames. `completed`, `unavailable` and
  // `fatal` do not — there is nothing left to lose.
  useHoldAppUpdate(
    state.phase === "active" ||
      state.phase === "submitting_move" ||
      state.phase === "reconnecting" ||
      state.phase === "resyncing" ||
      state.activeCommand !== null,
  );

  const onSelect = (square: string) => {
    const completed = selection.select(square);
    if (completed === null) return;
    // Staged or submitted, never both: `stage` answers whether it took the
    // move, so the preference is read in one place rather than branched on
    // here and again inside the hook.
    if (!confirmation.stage(completed)) void submit(completed);
  };

  if (state.phase === "unavailable" || state.phase === "fatal") {
    return (
      <section className="mx-auto flex w-full max-w-md flex-col items-start gap-4 py-12">
        <h1 className="text-xl font-semibold">{t("game.title")}</h1>
        <p role="alert" className="text-sm">
          {t(state.phase === "fatal" ? "game.errors.fatal" : "game.errors.unavailable")}
        </p>
        <Button asChild variant="outline" className="min-h-11">
          <Link to="/play">{t("game.result.backToLobby")}</Link>
        </Button>
      </section>
    );
  }

  return (
    <section className="mx-auto flex w-full max-w-5xl flex-col gap-4 lg:flex-row lg:items-start">
      <h1 className="sr-only">{t("game.title")}</h1>

      {/* A64-025.6 §4, §6. The board column carries the two seats, so a
          clock is adjacent to the board at **every** width — that is the
          answer to OQ-3, and it is why the seats are here rather than in
          the panel beside it. On a phone there is no panel to hide them in;
          on a desktop they stay with the thing they are about. */}
      <div className="flex w-full min-w-0 flex-col gap-2 lg:max-w-[min(70vh,42rem)] lg:flex-1">
        {/* A64-023.2 §6, kept: the message sits beside the seat that sent
            it, so nothing overlays a board square or a clock. It moved here
            with the seats rather than staying in the panel. */}
        {farBubble !== undefined && (
          <QuickMessageBubble key={farBubble.key} bubble={farBubble} align="far" />
        )}

        {state.side !== null && (
          <PlayerSeat
            side={far}
            identity={identities.get(state.participants?.[far] ?? "")}
            rating={state.ratings?.[far] ?? null}
            ms={far === "light" ? clock.lightMs : clock.darkMs}
            active={clock.activeSide === far}
            awaiting={clock.awaitingServer}
            isViewer={false}
            running={running}
          />
        )}

        {state.side === null ? (
          <Skeleton className="aspect-square w-full" aria-label={t("game.loading")} />
        ) : (
          <GameBoard
            board={state.board}
            orientation={state.side}
            movable={selection.movable}
            selected={confirmation.staged ?? selection.path}
            destinations={selection.destinations}
            captured={selection.captured}
            lastMove={state.lastMove}
            interactive={interactive}
            onSelect={onSelect}
          />
        )}

        {confirmation.staged !== null && (
          <PendingMove
            onConfirm={confirmation.confirm}
            onCancel={confirmation.cancel}
            disabled={state.phase === "submitting_move"}
          />
        )}

        {state.side !== null && (
          <PlayerSeat
            side={near}
            identity={identities.get(state.participants?.[near] ?? "")}
            rating={state.ratings?.[near] ?? null}
            ms={near === "light" ? clock.lightMs : clock.darkMs}
            active={clock.activeSide === near}
            awaiting={clock.awaitingServer}
            isViewer
            running={running}
          />
        )}

        {nearBubble !== undefined && (
          <QuickMessageBubble key={nearBubble.key} bubble={nearBubble} align="near" />
        )}
      </div>

      {/* A64-025.6A §15. One surface, not three.
          The status was a card, the quick-message controls were two bare
          buttons floating on the page background, and the game controls
          were a second card — three unrelated blocks in a column, which is
          what made the right-hand side read as leftovers rather than as the
          game's controls. They are one panel now, with a divider between
          the two groups and a hierarchy inside each: destructive last,
          contextual first. */}
      <aside className="border-border bg-card flex w-full flex-col divide-y rounded-xl border shadow-sm lg:w-80 lg:shrink-0">
        <div className="p-4">
          <GamePanel
            state={state}
            connection={connection}
            viewerId={isAuthenticated(session) ? session.user.id : null}
          />
        </div>

        {/* Only for a participant. A spectator has no seat, so there is
            nobody for a message to come from and nothing to mute. */}
        {state.side !== null && (
          <div className="p-4">
            <QuickMessagePicker
              disabled={!quickMessages.canSend}
              muted={quickMessages.muted}
              error={quickMessages.error}
              onSelect={quickMessages.send}
              onToggleMute={quickMessages.toggleMute}
            />
          </div>
        )}

        <div className="p-4">
          <GameControls state={state} onCommand={(kind) => void command(kind)} />
        </div>
      </aside>
    </section>
  );
}
