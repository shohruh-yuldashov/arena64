import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useState } from "react";

import type { PendingMatch } from "@/entities/queue";
import { formatMillis } from "@/entities/time-control";
import { FormError } from "@/features/auth/ui/form-status";
import { queueErrorKey } from "@/features/matchmaking/model/error-messages";
import { useAcceptMatch, useDeclineMatch } from "@/features/matchmaking/model/queries";
import { useCountdown } from "@/features/matchmaking/model/use-countdown";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Avatar, AvatarFallback, Button, Spinner } from "@/shared/ui";

/**
 * "You have been paired" — A64-020.5A §14, §15, §16, §17.
 *
 * ## A dialog that behaves like an alert dialog
 *
 * §14 asks for interruption semantics, and this is Radix's `Dialog`
 * configured to have them rather than `@radix-ui/react-alert-dialog`, which
 * is not a dependency of this app and is not worth becoming one for the
 * three lines below: `role="alertdialog"`, `Escape` prevented,
 * outside-click prevented, and no close affordance.
 *
 * Those three are the whole of the difference that matters here. The offer
 * carries a thirty-second deadline, so a dismissal would leave a player
 * with a match they never answered quietly expiring behind a page that
 * looks idle — the ambiguity §14 forbids. The only ways out are Accept and
 * Decline, which is what an alert dialog means.
 *
 * Everything else — the focus trap, the return of focus on close,
 * `aria-modal`, hiding the rest of the page from assistive technology — is
 * Radix's and is identical between the two primitives.
 *
 * The shared `DialogContent` is deliberately **not** reused: it renders a
 * close button, which is precisely the affordance this must not have.
 *
 * ## Everything shown is already in hand
 *
 * `PendingMatchResponse` carries the opponent's public summary, the
 * variant, the mode, the clock and the deadline, so this component makes
 * **no requests of its own**. §25 forbids a per-offer profile or rating
 * read and there is none: one offer is one player, and fetching them would
 * be an N+1 with N=1 that becomes an N+1 with N=many the day a lobby shows
 * more than one.
 *
 * Two things §14 lists are **absent because the API does not send them**:
 * the opponent's avatar thumbnail and their rating. `OpponentPreview` is
 * three fields by deliberate design — rendering an avatar needs the storage
 * provider and the privacy-gated composition `profiles` owns, and doing it
 * here would be a second, ungated renderer of a player's identity. Nothing
 * is inferred to fill the gap: an initial-letter fallback is not a guess
 * about somebody's picture.
 */
export function MatchOfferDialog({
  match,
  onExpired,
  onAccepted,
}: {
  match: PendingMatch;
  /** The countdown reached zero locally. Re-read; do not conclude. */
  onExpired: () => void;
  /** Both players accepted. The page navigates. */
  onAccepted: (matchId: string) => void;
}) {
  const { t, locale } = useTranslation();
  const accept = useAcceptMatch();
  const decline = useDeclineMatch();
  const [failure, setFailure] = useState<TranslationKey | null>(null);
  const countdown = useCountdown(match.acceptance_deadline, onExpired);

  const busy = accept.isPending || decline.isPending;
  const clock = formatMillis(match.base_time_ms, match.increment_ms, locale);
  const opponent = match.opponent;

  const onAccept = async () => {
    setFailure(null);
    try {
      const answered = await accept.mutateAsync(match.match_id);
      // The server's own word for "both of you agreed". Deriving it from
      // `you_accepted && opponent_accepted` would be a second definition of
      // activation, and this one is the record's.
      if (answered.status === "active") onAccepted(answered.match_id);
    } catch (error) {
      setFailure(queueErrorKey(error, "match"));
    }
  };

  const onDecline = async () => {
    setFailure(null);
    try {
      await decline.mutateAsync(match.match_id);
    } catch (error) {
      setFailure(queueErrorKey(error, "match"));
    }
  };

  return (
    <DialogPrimitive.Root open>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/60" />
        <DialogPrimitive.Content
          role="alertdialog"
          onEscapeKeyDown={(event) => event.preventDefault()}
          onPointerDownOutside={(event) => event.preventDefault()}
          onInteractOutside={(event) => event.preventDefault()}
          className="bg-background fixed inset-x-0 bottom-0 z-50 flex max-h-[90dvh] flex-col gap-4 overflow-y-auto rounded-t-lg border p-6 pb-[max(1.5rem,env(safe-area-inset-bottom))] sm:inset-1/2 sm:bottom-auto sm:w-full sm:max-w-md sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-lg"
        >
          <DialogPrimitive.Title className="text-lg font-semibold">
            {t("play.offer.title")}
          </DialogPrimitive.Title>

          <DialogPrimitive.Description className="text-muted-foreground text-sm">
            {t("play.offer.description")}
          </DialogPrimitive.Description>

          <div className="flex items-center gap-3">
            <Avatar className="size-10 shrink-0">
              <AvatarFallback aria-hidden="true">
                {(opponent?.display_name ?? opponent?.username ?? "?")
                  .slice(0, 2)
                  .toUpperCase()}
              </AvatarFallback>
            </Avatar>
            <div className="flex min-w-0 flex-col">
              <span className="truncate text-sm font-medium">
                {opponent?.display_name ??
                  opponent?.username ??
                  t("play.offer.unknownOpponent")}
              </span>
              {opponent !== null && (
                <span className="text-muted-foreground truncate text-xs">
                  @{opponent.username}
                </span>
              )}
            </div>
          </div>

          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">{t("play.form.mode")}</dt>
            <dd className="font-medium">
              {t(match.rated ? "play.mode.ranked" : "play.mode.casual")}
            </dd>
            <dt className="text-muted-foreground">{t("play.form.timeControl")}</dt>
            <dd className="font-medium tabular-nums">{clock ?? "—"}</dd>
            {match.speed_class !== null && (
              <>
                <dt className="text-muted-foreground">{t("play.waiting.speed")}</dt>
                <dd className="font-medium">
                  {t(SPEED_LABELS[match.speed_class] ?? "play.speed.unknown")}
                </dd>
              </>
            )}
          </dl>

          {/* The countdown. `aria-live="off"` on the number itself — §15
              and §23 both forbid announcing every second — and a separate
              polite region that speaks only at 30, 20, 10 and 5. */}
          <p className="text-sm">
            <span aria-live="off" className="text-2xl font-semibold tabular-nums">
              {new Intl.NumberFormat(locale).format(countdown.secondsLeft)}
            </span>{" "}
            <span className="text-muted-foreground">{t("play.offer.secondsLeft")}</span>
          </p>
          <p aria-live="polite" className="sr-only">
            {countdown.announcement === null
              ? ""
              : t("play.offer.announce", { seconds: String(countdown.announcement) })}
          </p>

          {match.you_accepted && (
            <p role="status" className="text-muted-foreground text-sm">
              {t("play.offer.awaitingOpponent")}
            </p>
          )}

          {failure !== null && <FormError messageKey={failure} />}

          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button
              variant="outline"
              className="min-h-11"
              disabled={busy}
              // Named with the opponent, so a screen reader hears "Decline,
              // Ali" rather than a bare verb with no object.
              aria-label={t("play.offer.declineLabel", {
                name: opponent?.username ?? t("play.offer.unknownOpponent"),
              })}
              onClick={() => void onDecline()}
            >
              {decline.isPending ? (
                <Spinner label={t("play.offer.declining")} />
              ) : (
                t("play.offer.decline")
              )}
            </Button>
            <Button
              className="min-h-11"
              disabled={busy || match.you_accepted}
              aria-label={t("play.offer.acceptLabel", {
                name: opponent?.username ?? t("play.offer.unknownOpponent"),
              })}
              onClick={() => void onAccept()}
            >
              {accept.isPending ? (
                <Spinner label={t("play.offer.accepting")} />
              ) : (
                t("play.offer.accept")
              )}
            </Button>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

const SPEED_LABELS: Partial<Record<NonNullable<PendingMatch["speed_class"]>, TranslationKey>> =
  {
    bullet: "play.speed.bullet",
    blitz: "play.speed.blitz",
    rapid: "play.speed.rapid",
    classical: "play.speed.classical",
    correspondence: "play.speed.correspondence",
  };
