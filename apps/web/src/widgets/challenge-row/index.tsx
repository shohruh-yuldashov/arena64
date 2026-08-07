import { nameOf } from "@/entities/profile";
import { formatTimeControl, type TimeControl } from "@/entities/time-control";
import type { Challenge } from "@/features/challenges/api";
import { useExpiry } from "@/features/challenges/model/use-expiry";
import {
  ChallengeActions,
  type ChallengeActionSet,
} from "@/features/challenges/ui/challenge-actions";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { PlayerRow } from "@/widgets/player-row";

/**
 * One challenge, in a dense list — A64-022.5 §3.
 *
 * A widget rather than a feature component, because it **composes** two:
 * `PlayerRow`, which is the one place presence and avatars are rendered,
 * and `ChallengeActions`, which is the one place a challenge transition is
 * written. Features may not import widgets, so the composition lives at the
 * layer that may import both.
 *
 * The other party is a `ProfileResponse` — the same shape the friends list,
 * both request lists and search render — so it goes through the same row,
 * with presence gated where it is already gated. A second row component
 * would be a second answer to "is this player online", and the first one to
 * drift would win on whichever screen read it.
 *
 * ## What the terms line says, and what it does not
 *
 * The clock, the mode, and how long is left. Not the variant:
 * `ProductVariant` has exactly one member, and a line that always reads the
 * same teaches a reader to stop reading it. It joins the moment there are
 * two.
 *
 * The clock is formatted from the **catalogue**, looked up by the
 * challenge's `time_control_id`. A challenge stores the code rather than the
 * durations, so a control the catalogue no longer offers renders as its bare
 * code — true, and better than inventing numbers for it.
 *
 * ## Expiry is display, not authority — §10
 *
 * `useExpiry` counts against the **local** clock, so a device two minutes
 * fast reaches zero early. All that does is disable the buttons and change
 * the meta line; the row is removed by the next read, and the server is what
 * refuses a late answer. Nothing here cancels, hides or rewrites a
 * challenge on a timer.
 */
export function ChallengeRow({
  challenge,
  controls,
  actions,
}: {
  challenge: Challenge;
  /** The catalogue, for the clock. Absent while it loads. */
  controls: readonly TimeControl[] | undefined;
  actions: ChallengeActionSet;
}) {
  const { t, locale } = useTranslation();
  const expiry = useExpiry(challenge.expires_at);

  const control = controls?.find((entry) => entry.id === challenge.time_control_id);
  const clock = control ? formatTimeControl(control, locale) : challenge.time_control_id;
  const name = nameOf(challenge.player);

  return (
    <PlayerRow
      player={challenge.player}
      meta={
        expiry.isExpired
          ? t("challenges.row.expired")
          : t("challenges.row.terms", {
              clock,
              mode: t(challenge.rated ? "play.mode.ranked" : "play.mode.casual"),
              remaining: remainingLabel(expiry.minutesLeft, t, locale),
            })
      }
      actions={
        <ChallengeActions
          challengeId={challenge.id}
          playerName={name}
          actions={actions}
          // Only an incoming row is answerable, so only it is disabled by a
          // lapsed window. Cancelling a challenge you sent stays available:
          // the row is yours, and withdrawing it is not an answer.
          disabled={actions.kind === "incoming" && expiry.isExpired}
        />
      }
    />
  );
}

/**
 * "3h left" or "12m left" — the bucket, in the reader's own numbering system.
 *
 * Hours above an hour, minutes below it. `Intl.RelativeTimeFormat` was the
 * alternative and produces "in 3 hours", which is a sentence where this is a
 * **label on a dense row** — and which would say "in 0 hours" for the
 * fifty-nine minutes it rounds away.
 */
function remainingLabel(
  minutesLeft: number,
  t: (key: TranslationKey, values?: Record<string, string | number>) => string,
  locale: string,
): string {
  const format = (value: number) => new Intl.NumberFormat(locale).format(value);
  return minutesLeft >= 60
    ? t("challenges.row.hoursLeft", { hours: format(Math.floor(minutesLeft / 60)) })
    : t("challenges.row.minutesLeft", { minutes: format(minutesLeft) });
}
