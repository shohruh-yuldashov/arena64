import { Link, useParams } from "@tanstack/react-router";

import { useTranslation } from "@/shared/i18n";
import { Button, Card, CardContent } from "@/shared/ui";

/**
 * `/games/$matchId` — the handoff, and nothing more (A64-020.5A §19).
 *
 * ## What this deliberately is not
 *
 * Not a board, not a move list, not a clock, not a WebSocket. §1 excludes
 * every one of them and this page implements none: it makes **no requests
 * at all**, so there is no snapshot read, no subscription and no way for it
 * to imply that gameplay works.
 *
 * ## Why it exists at all, then
 *
 * Because the route has to. Acceptance produces a match identifier and the
 * lobby navigates with it; without a registered route that is a 404 at the
 * end of a successful pairing, and the E2E flow §27 asks for could not
 * assert that two players reached the same place.
 *
 * So this holds the URL and says plainly what it is. The identifier is read
 * from the path and rendered, which is what makes it verifiable that the
 * right match was handed off — and A64-020.5B replaces the body of this
 * file without touching the route, the guard or the navigation that reaches
 * it.
 *
 * The state is **labelled as development**, not dressed up as a loading
 * screen. A spinner here would be a lie that resolves to nothing.
 */
export default function GameReadyPage() {
  const { t } = useTranslation();
  const { matchId } = useParams({ from: "/games/$matchId" });

  return (
    <section className="mx-auto flex w-full max-w-2xl flex-col gap-6">
      <h1 className="text-2xl font-semibold tracking-tight">{t("play.game.title")}</h1>

      <Card>
        <CardContent className="flex flex-col gap-4 pt-6">
          <p className="text-sm">{t("play.game.ready")}</p>

          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">{t("play.game.matchId")}</dt>
            {/* `break-all` because a UUID has no break opportunities and
                would otherwise overflow a 360px viewport. */}
            <dd className="font-mono text-xs break-all">{matchId}</dd>
          </dl>

          <p className="text-muted-foreground text-sm">{t("play.game.boardPending")}</p>

          <Button asChild variant="outline" className="min-h-11 self-start">
            <Link to="/play">{t("play.game.backToLobby")}</Link>
          </Button>
        </CardContent>
      </Card>
    </section>
  );
}
