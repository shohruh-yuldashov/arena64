import { Link, useParams } from "@tanstack/react-router";
import { ChevronLeftIcon } from "lucide-react";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { GameBoard } from "@/features/game/ui/board";
import { refusalOf, useReplay } from "@/features/replay/model/queries";
import { useReplayNavigation } from "@/features/replay/model/state";
import { useReplayShortcuts } from "@/features/replay/model/use-replay-shortcuts";
import { MoveList } from "@/features/replay/ui/move-list";
import { ReplayControls } from "@/features/replay/ui/replay-controls";
import { ReplaySummary } from "@/features/replay/ui/replay-summary";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Skeleton } from "@/shared/ui";

/**
 * One finished game, played back — A64-020.5E §2, §7, §16, §17, §19.
 *
 * ## No socket, no engine, one request
 *
 * A replay is an immutable HTTP document. This page opens no WebSocket
 * (§2), replays nothing locally (§8) and issues exactly one request for the
 * whole game — every ply carries the board it produced, so stepping through
 * a hundred positions costs nothing.
 *
 * ## The board is the live one, read-only
 *
 * `GameBoard` unchanged, with `interactive={false}` and no movable squares.
 * §7 asks for reuse rather than a fork precisely so that a position renders
 * identically here and in a live game — a second board would be a second
 * coordinate mapping, and the day they disagreed the archive would be
 * wrong about a game that was played correctly.
 *
 * Nothing live comes with it: no legal-move generation, no turn, no clock,
 * no pending move. Those are `useGameRoom`'s and this page never mounts it.
 */
export default function ReplayPage() {
  const { t } = useTranslation();
  const { matchId } = useParams({ from: "/games/$matchId/replay" });
  const { state: session } = useSession();
  const viewerId = isAuthenticated(session) ? session.user.id : null;

  const { data: replay, isPending, isError, error, refetch } = useReplay(matchId);

  if (isPending) {
    return (
      <section className="mx-auto flex w-full max-w-5xl flex-col gap-4 py-6 lg:flex-row">
        <Skeleton className="aspect-square w-full lg:max-w-[min(70vh,40rem)] lg:flex-1" />
        <Skeleton className="h-64 w-full lg:w-80" />
        <span className="sr-only">{t("replay.loading")}</span>
      </section>
    );
  }

  if (isError) return <Refusal kind={refusalOf(error)} onRetry={() => void refetch()} />;

  return <Replay replay={replay} viewerId={viewerId} />;
}

/**
 * Everything that is not a replay — §16, §17, §18.
 *
 * Three states, and the distinctions matter more than the copy:
 *
 * `not_found` covers a match that does not exist **and** a casual match the
 * viewer did not play. The backend gives one answer for both deliberately,
 * and this gives one screen for both — anything that said "you do not have
 * permission" would confirm the match exists, which is exactly what the
 * indistinguishability is for.
 *
 * `unsupported_engine_version` is **not an error**. The match exists and
 * the viewer may see it; this build declines to reconstruct a game played
 * under rules that have since been fixed, because a reconstruction could
 * end differently from the game that was rated and displayed. No board is
 * shown — an empty one pretending to be valid is the failure this refuses.
 */
function Refusal({
  kind,
  onRetry,
}: {
  kind: ReturnType<typeof refusalOf>;
  onRetry: () => void;
}) {
  const { t } = useTranslation();

  const copy: Record<typeof kind, { title: TranslationKey; body: TranslationKey }> = {
    not_found: { title: "replay.notFound.title", body: "replay.notFound.body" },
    unsupported_engine_version: {
      title: "replay.unsupported.title",
      body: "replay.unsupported.body",
    },
    unexpected: { title: "replay.error.title", body: "replay.error.body" },
  };

  return (
    <section className="mx-auto flex w-full max-w-md flex-col items-start gap-4 py-12">
      <h1 className="text-xl font-semibold">{t("replay.title")}</h1>
      <div role="alert" className="flex flex-col gap-2">
        <p className="font-medium">{t(copy[kind].title)}</p>
        <p className="text-muted-foreground text-sm">{t(copy[kind].body)}</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {/* Retry only where retrying can help. A `404` and a refused engine
            version are stable answers about a permanent record. */}
        {kind === "unexpected" && (
          <Button variant="outline" className="min-h-11" onClick={onRetry}>
            {t("state.retry")}
          </Button>
        )}
        <Button asChild variant="outline" className="min-h-11">
          <Link to="/play">{t("game.result.backToLobby")}</Link>
        </Button>
      </div>
    </section>
  );
}

function Replay({
  replay,
  viewerId,
}: {
  replay: NonNullable<ReturnType<typeof useReplay>["data"]>;
  viewerId: string | null;
}) {
  const { t } = useTranslation();
  const view = useReplayNavigation(replay, viewerId);
  useReplayShortcuts(view, true);

  return (
    <section className="mx-auto flex w-full max-w-6xl flex-col gap-4 py-4">
      {/* A64-025.5C §23. The page opened on a board with no heading and no
          way back: a player arrives here from match history and the only
          route out was the browser's own button. The `h1` was `sr-only`,
          which told a screen reader where it was and nobody else. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold tracking-tight">{t("replay.title")}</h1>
        <Button asChild variant="ghost" size="sm" className="min-h-11">
          <Link to="/games/history">
            <ChevronLeftIcon aria-hidden="true" className="size-4" />
            {t("replay.backToHistory")}
          </Link>
        </Button>
      </div>

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        {/* Board first in the DOM on every width — it is the content, and on
          a phone it is what should be under the thumb. */}
        <div className="flex w-full min-w-0 flex-col gap-4 lg:max-w-[min(70vh,40rem)] lg:flex-1">
          <GameBoard
            board={view.position.board}
            orientation={view.orientation}
            // Read-only: nothing is movable, nothing is a destination, and
            // `interactive={false}` disables every cell (§7).
            movable={[]}
            selected={[]}
            destinations={[]}
            captured={[]}
            lastMove={
              view.position.playedPath === null
                ? null
                : { path: view.position.playedPath, captured: view.position.captured }
            }
            interactive={false}
            onSelect={() => undefined}
          />
          <ReplayControls view={view} />
        </div>

        <div className="flex w-full flex-col gap-4 lg:w-80 lg:shrink-0">
          <ReplaySummary replay={replay} />
          <MoveList plies={replay.plies} view={view} />
        </div>
      </div>
    </section>
  );
}
