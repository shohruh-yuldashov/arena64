import { Link } from "@tanstack/react-router";

import { usePreferences } from "@/features/profile/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui";
import { BoardSample } from "@/widgets/board-preview/board-sample";

/**
 * The board the player will actually get — A64-025.5B §22.
 *
 * ## Why it is in the lobby
 *
 * `board_theme` and `piece_set` were chosen on a settings page and shown
 * nowhere: a player picked "Wood" and had to start a game to find out what
 * they had picked. The lobby is the last screen before a board, which makes
 * it the one place a preview answers a question somebody actually has.
 *
 * ## It renders nothing of its own
 *
 * `BoardSample` is drawn with the same elements and the same classes the
 * game room's board uses, and `GameplayPreferences` has already put the
 * player's choice on the document. So this preview is correct without
 * knowing which theme is selected, and it stays correct when a theme or a
 * piece set is added — which is the whole point of doing this in tokens
 * rather than in props.
 *
 * The names beside it do need the values, and they are translated rather
 * than printed: `classic` and `midnight` are server enums, and A64-025.9C
 * was the third time this product shipped one of those to a screen.
 */
export function BoardPreview() {
  const { t } = useTranslation();
  const preferences = usePreferences();
  const gameplay = preferences.data?.gameplay;

  // Nothing at all until the preference has arrived. A preview of the
  // default that then changes under the reader is worse than a preview that
  // appears — the same call `StandingStrip` makes on the home page.
  if (gameplay === undefined) return null;

  return (
    <section
      aria-labelledby="board-preview-heading"
      className="border-border bg-card flex items-center gap-4 rounded-xl border p-4 sm:p-5"
    >
      <BoardSample className="size-20 shrink-0 sm:size-24" />

      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <h2 id="board-preview-heading" className="text-sm font-semibold">
          {t("play.boardTitle")}
        </h2>
        <p className="text-muted-foreground text-xs">
          {t("play.boardBody", {
            theme: t(
              `profile.preferences.boardThemes.${gameplay.board_theme}` as TranslationKey,
            ),
            pieces: t(`profile.preferences.pieceSets.${gameplay.piece_set}` as TranslationKey),
          })}
        </p>
        <Button asChild variant="ghost" size="sm" className="mt-1 min-h-11 self-start px-2">
          <Link to="/settings/preferences">{t("play.boardCta")}</Link>
        </Button>
      </div>
    </section>
  );
}
