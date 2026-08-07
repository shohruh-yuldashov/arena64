import { useState } from "react";

import { FormError } from "@/features/auth/ui/form-status";
import { challengeErrorKey } from "@/features/challenges/model/error-messages";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";

/**
 * What a challenge row can do, by side — A64-022.5 §3, §8, §9.
 *
 * The feature half of a row. Layout is `widgets/challenge-row`'s, which
 * composes this with `PlayerRow` — features may not import widgets, and the
 * split is the layering rather than a preference: this component knows
 * about accepting and declining, and nothing about how a player is drawn.
 *
 * ## No confirmation on any of the three — §8, §9
 *
 * None is destructive in the way `RelationshipActions` guards against.
 * Removing a friend or blocking somebody changes a lasting relationship;
 * declining an invitation ends something that was going to end in
 * twenty-four hours anyway. A dialog per decline would make the common
 * answer the slower one.
 *
 * The row disappears **after** the call succeeds, which is the only safe
 * reading of "optimistic removal" here: the list is invalidated and the
 * server's next answer removes it. Splicing it out of a local array before
 * the call returned would leave a failed decline showing nothing at all.
 *
 * ## The busy guard is local, deliberately
 *
 * §5's duplicate-submit prevention, and it is `useState` rather than a
 * mutation's `isPending` because every row on the page shares one mutation
 * hook — a pending decline would otherwise disable the buttons on all of
 * them.
 */
export type ChallengeActionSet =
  | {
      kind: "incoming";
      onAccept: (challengeId: string) => Promise<unknown>;
      onDecline: (challengeId: string) => Promise<unknown>;
    }
  | { kind: "outgoing"; onCancel: (challengeId: string) => Promise<unknown> };

export function ChallengeActions({
  challengeId,
  playerName,
  actions,
  disabled = false,
}: {
  challengeId: string;
  /** Named in every label, so a screen reader hears "Accept, Ali". */
  playerName: string;
  actions: ChallengeActionSet;
  /** The local clock says the window closed. A courtesy — the server decides. */
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const [failure, setFailure] = useState<TranslationKey | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (action: (challengeId: string) => Promise<unknown>) => {
    if (busy) return;
    setFailure(null);
    setBusy(true);
    try {
      await action(challengeId);
    } catch (error) {
      setFailure(challengeErrorKey(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex gap-2">
        {actions.kind === "incoming" ? (
          <>
            <Button
              variant="outline"
              size="sm"
              className="min-h-11"
              disabled={busy || disabled}
              aria-label={t("challenges.actions.declineLabel", { name: playerName })}
              onClick={() => void run(actions.onDecline)}
            >
              {t("challenges.actions.decline")}
            </Button>
            <Button
              size="sm"
              className="min-h-11"
              disabled={busy || disabled}
              aria-label={t("challenges.actions.acceptLabel", { name: playerName })}
              onClick={() => void run(actions.onAccept)}
            >
              {busy ? (
                <Spinner label={t("challenges.actions.accepting")} />
              ) : (
                t("challenges.actions.accept")
              )}
            </Button>
          </>
        ) : (
          <Button
            variant="outline"
            size="sm"
            className="min-h-11"
            disabled={busy}
            aria-label={t("challenges.actions.cancelLabel", { name: playerName })}
            onClick={() => void run(actions.onCancel)}
          >
            {busy ? (
              <Spinner label={t("challenges.actions.cancelling")} />
            ) : (
              t("challenges.actions.cancel")
            )}
          </Button>
        )}
      </div>
      {failure !== null && <FormError messageKey={failure} />}
    </div>
  );
}
