import { useState } from "react";

import { formatTimeControl, type TimeControlId } from "@/entities/time-control";
import { FormError } from "@/features/auth/ui/form-status";
import { DEFAULT_VARIANT } from "@/features/challenges/api";
import { challengeErrorKey } from "@/features/challenges/model/error-messages";
import { useCreateChallenge } from "@/features/challenges/model/queries";
import { useTimeControls } from "@/features/matchmaking/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Skeleton,
  Spinner,
} from "@/shared/ui";

/**
 * "Challenge Ali to a game" — A64-022.5 §4, §5.
 *
 * ## The opponent is not a choice
 *
 * §4: no manual player selection. The dialog is opened *from* a friend —
 * a row in the friends list, or their profile — so the opponent is a
 * `player_id` **the server previously returned**, passed in as a prop and
 * shown as a name the caller supplies. There is no field to type a username
 * into, which is why there is nothing here to validate about one.
 *
 * ## Two choices, and the two that are deliberately absent
 *
 * The clock and the mode, exactly as `QueueForm` offers them, and the same
 * two omissions for the same reasons: `ProductVariant` has one member, so a
 * radio group would be a control that can only be left where it was; and
 * region is a queue concern that does not exist for a directed invitation.
 *
 * The clock has **no default** and Send stays disabled until one is picked.
 * `QueueForm`'s argument transfers exactly: every control is a genuinely
 * different game, so pre-selecting one would make the most consequential
 * choice the one that took the fewest clicks. Mode defaults to casual,
 * which is the safer of the two to land on because its result does not move
 * a permanent number.
 *
 * ## Validation is the backend's — §5
 *
 * Nothing here checks friendship, blocking, duplicates or the catalogue.
 * Each is re-evaluated inside the create transaction against state that can
 * have changed since this dialog opened, so a client-side copy would be a
 * second answer whose only distinction is being older. What this does is
 * submit and render what comes back — `challengeErrorKey` maps all six
 * challenge codes.
 *
 * ## Duplicate submits
 *
 * The form is disabled while the mutation is in flight and the dialog
 * closes on success, so the button cannot be pressed twice. That is the
 * client half; the backend's half is `uq_friend_challenge__live_pair`,
 * which refuses a second live challenge between the same two people
 * whatever a client does.
 *
 * ## The modal itself is Radix's
 *
 * `DialogContent` from `shared/ui`, unchanged — the focus trap, the return
 * of focus to the trigger on close, `Escape`, `aria-modal` and hiding the
 * rest of the page from assistive technology are all Radix's. §5 forbids a
 * new modal framework and there was never a reason to want one.
 */
export function CreateChallengeDialog({
  open,
  onOpenChange,
  opponentId,
  opponentName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  opponentId: string;
  opponentName: string;
}) {
  const { t, locale } = useTranslation();
  const controls = useTimeControls();
  const create = useCreateChallenge();

  const [timeControlId, setTimeControlId] = useState<TimeControlId | null>(null);
  const [rated, setRated] = useState(false);
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const submit = async () => {
    if (timeControlId === null || create.isPending) return;
    setFailure(null);
    try {
      await create.mutateAsync({
        recipient_id: opponentId,
        time_control_id: timeControlId,
        // Not a choice — see `DEFAULT_VARIANT` on why it is sent anyway.
        variant: DEFAULT_VARIANT,
        rated,
      });
      // Reset before closing, so re-opening for a different friend does not
      // inherit the last choice — a clock is a per-game decision, and a
      // remembered one is the pre-selection this dialog refuses to make.
      setTimeControlId(null);
      setRated(false);
      onOpenChange(false);
    } catch (error) {
      setFailure(challengeErrorKey(error));
    }
  };

  return (
    <Dialog open={open} onOpenChange={create.isPending ? undefined : onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t("challenges.create.title", { name: opponentName })}</DialogTitle>
          <DialogDescription>{t("challenges.create.description")}</DialogDescription>
        </DialogHeader>

        <fieldset disabled={create.isPending} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <span id="challenge-mode-label" className="text-sm font-medium">
              {t("play.form.mode")}
            </span>
            {/* A radio group rather than a switch: "rated" and "casual" are
                two named things, and a switch labelled "rated" makes the
                other one the absence of a label. */}
            <div
              role="radiogroup"
              aria-labelledby="challenge-mode-label"
              className="flex gap-2"
            >
              {([false, true] as const).map((value) => (
                <button
                  key={String(value)}
                  type="button"
                  role="radio"
                  aria-checked={rated === value}
                  onClick={() => setRated(value)}
                  className={cn(
                    "focus-visible:ring-ring min-h-11 flex-1 rounded-md border px-3 text-sm focus-visible:ring-2 focus-visible:outline-none",
                    rated === value && "border-primary bg-muted font-medium",
                  )}
                >
                  {t(value ? "play.mode.ranked" : "play.mode.casual")}
                </button>
              ))}
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <span id="challenge-clock-label" className="text-sm font-medium">
              {t("play.form.timeControl")}
            </span>
            {controls.isPending ? (
              <Skeleton className="h-11 w-full" />
            ) : controls.isError ? (
              <FormError messageKey="challenges.errors.catalogue" />
            ) : (
              <div
                role="radiogroup"
                aria-labelledby="challenge-clock-label"
                className="grid grid-cols-2 gap-2"
              >
                {(controls.data ?? []).map((control) => (
                  <button
                    key={control.id}
                    type="button"
                    role="radio"
                    aria-checked={timeControlId === control.id}
                    onClick={() => setTimeControlId(control.id)}
                    className={cn(
                      "focus-visible:ring-ring min-h-11 rounded-md border px-3 text-sm tabular-nums focus-visible:ring-2 focus-visible:outline-none",
                      timeControlId === control.id && "border-primary bg-muted font-medium",
                    )}
                  >
                    {formatTimeControl(control, locale)}
                  </button>
                ))}
              </div>
            )}
          </div>
        </fieldset>

        {failure !== null && <FormError messageKey={failure} />}

        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button
            variant="outline"
            className="min-h-11"
            disabled={create.isPending}
            onClick={() => onOpenChange(false)}
          >
            {t("common.cancel")}
          </Button>
          <Button
            className="min-h-11"
            disabled={timeControlId === null || create.isPending}
            onClick={() => void submit()}
          >
            {create.isPending ? (
              <Spinner label={t("challenges.create.sending")} />
            ) : (
              t("challenges.create.send")
            )}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
