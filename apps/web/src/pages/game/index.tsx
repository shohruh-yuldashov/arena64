import { Link, useParams } from "@tanstack/react-router";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { canInteract } from "@/features/game/model/state";
import { useClock } from "@/features/game/model/use-clock";
import { useGameRoom } from "@/features/game/model/use-game-room";
import { useMoveSelection } from "@/features/game/model/use-move-selection";
import { GameBoard } from "@/features/game/ui/board";
import { GameControls } from "@/features/game/ui/game-controls";
import { GamePanel } from "@/features/game/ui/game-panel";
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

  // The countdown runs only while a game is genuinely in progress. A
  // completed game's clock is frozen at whatever the last frame said, which
  // is the honest reading — §19's "paused/completed handling".
  const clock = useClock(
    state.clock,
    state.phase === "active" || state.phase === "submitting_move",
  );

  const interactive = canInteract(state);

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
    if (completed !== null) void submit(completed);
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

      <div className="w-full min-w-0 lg:max-w-[min(70vh,42rem)] lg:flex-1">
        {state.side === null ? (
          <Skeleton className="aspect-square w-full" aria-label={t("game.loading")} />
        ) : (
          <GameBoard
            board={state.board}
            orientation={state.side}
            movable={selection.movable}
            selected={selection.path}
            destinations={selection.destinations}
            captured={selection.captured}
            lastMove={state.lastMove}
            interactive={interactive}
            onSelect={onSelect}
          />
        )}
      </div>

      {/* The panel and the controls stack in one column: beside the board
          on a wide screen, below it on a phone — one `lg:` breakpoint, as
          the board already uses (A64-020.5C §14). The controls come second
          so the clocks are never pushed off screen by an incoming offer. */}
      <div className="flex w-full flex-col gap-4 lg:w-80 lg:shrink-0">
        <GamePanel
          state={state}
          clock={clock}
          connection={connection}
          viewerId={isAuthenticated(session) ? session.user.id : null}
        />
        <GameControls state={state} onCommand={(kind) => void command(kind)} />
      </div>
    </section>
  );
}
