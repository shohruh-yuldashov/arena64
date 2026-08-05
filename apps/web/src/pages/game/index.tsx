import { Link, useParams } from "@tanstack/react-router";

import { canInteract } from "@/features/game/model/state";
import { useClock } from "@/features/game/model/use-clock";
import { useGameRoom } from "@/features/game/model/use-game-room";
import { useMoveSelection } from "@/features/game/model/use-move-selection";
import { GameBoard } from "@/features/game/ui/board";
import { GamePanel } from "@/features/game/ui/game-panel";
import { useTranslation } from "@/shared/i18n";
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
 */
export default function GamePage() {
  const { t } = useTranslation();
  const { matchId } = useParams({ from: "/games/$matchId" });
  const connection = useConnectionStatus();
  const { state, submit } = useGameRoom(matchId);
  const selection = useMoveSelection(state);

  // The countdown runs only while a game is genuinely in progress. A
  // completed game's clock is frozen at whatever the last frame said, which
  // is the honest reading — §19's "paused/completed handling".
  const clock = useClock(
    state.clock,
    state.phase === "active" || state.phase === "submitting_move",
  );

  const interactive = canInteract(state);

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

      <div className="w-full lg:w-80 lg:shrink-0">
        <GamePanel state={state} clock={clock} connection={connection} />
      </div>
    </section>
  );
}
