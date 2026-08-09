import { useState } from "react";

import {
  type AdminSanction,
  MODERATION_CATEGORIES,
  type ModerationCategory,
  restoreAccount,
  restrictAccount,
} from "@/shared/api/client";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

/**
 * The two moderation controls, and the confirmation each needs — A64-024.6.
 *
 * Lives beside the account it acts on rather than on `/moderation`: an
 * operator restricts *this person*, having just read their page, and a
 * control on a list is one applied to whichever row was under the cursor.
 *
 * **No bulk action and no one-click action.** Both open a dialog that names
 * the target and states the consequence, because "revoke every session this
 * person holds" is not something to do by mis-clicking a row.
 *
 * ## The reason is a choice, not a sentence
 *
 * The category is a `<select>` over the server's closed vocabulary, and its
 * labels are localised here — the server stores `abuse`, the console shows
 * whatever the operator reads. The free-text field beside it is the
 * decision's *reasoning*, which is recorded on the case and never shown to
 * the restricted account.
 */

const CATEGORY_LABELS: Record<ModerationCategory, TranslationKey> = {
  cheating: "moderation.reasonCheating",
  abuse: "moderation.reasonAbuse",
  account_compromise: "moderation.reasonAccountCompromise",
  policy_violation: "moderation.reasonPolicyViolation",
  other: "moderation.reasonOther",
};

/** The durations offered, and the one that is not a duration at all. */
const DURATIONS: { hours: number | null; label: TranslationKey }[] = [
  { hours: null, label: "moderation.durationIndefinite" },
  { hours: 24, label: "moderation.duration24h" },
  { hours: 24 * 7, label: "moderation.duration7d" },
  { hours: 24 * 30, label: "moderation.duration30d" },
];

export function ModerationActions({
  userId,
  displayName,
  isRestricted,
  onChanged,
}: {
  userId: string;
  displayName: string;
  isRestricted: boolean;
  onChanged: (sanction: AdminSanction) => void;
}) {
  const { t } = useTranslation();

  const [open, setOpen] = useState<"restrict" | "restore" | null>(null);
  const [category, setCategory] = useState<ModerationCategory>("abuse");
  const [reasoning, setReasoning] = useState("");
  const [durationHours, setDurationHours] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    setOpen(null);
    setError(null);
    setReasoning("");
  };

  const run = async (action: () => ReturnType<typeof restoreAccount>) => {
    setBusy(true);
    setError(null);
    const outcome = await action();
    setBusy(false);

    if (outcome.status === "ok") {
      onChanged(outcome.value);
      close();
      return;
    }
    // The dialog stays open on failure, holding what was typed: a refusal
    // an operator has to re-enter from memory is a refusal they will get
    // wrong the second time.
    setError(t(outcome.status === "refused" ? "moderation.refused" : "moderation.failed"));
  };

  return (
    <>
      <p className="moderation-actions">
        {isRestricted ? (
          <button type="button" className="action" onClick={() => setOpen("restore")}>
            {t("moderation.restore")}
          </button>
        ) : (
          <button type="button" className="action danger" onClick={() => setOpen("restrict")}>
            {t("moderation.restrict")}
          </button>
        )}
      </p>

      <ConfirmDialog
        open={open === "restrict"}
        title={t("moderation.restrictTitle")}
        description={t("moderation.restrictBody", { name: displayName })}
        confirmLabel={t("moderation.restrict")}
        busy={busy}
        error={error}
        onCancel={close}
        onConfirm={() =>
          void run(() =>
            restrictAccount(userId, {
              category,
              reasoning,
              ...(durationHours !== null ? { duration_hours: durationHours } : {}),
            }),
          )
        }
      >
        <p className="field">
          <label htmlFor="restrict-category">{t("moderation.reason")}</label>
          <select
            id="restrict-category"
            value={category}
            onChange={(event) => setCategory(event.target.value as ModerationCategory)}
          >
            {MODERATION_CATEGORIES.map((value) => (
              <option key={value} value={value}>
                {t(CATEGORY_LABELS[value])}
              </option>
            ))}
          </select>
        </p>

        <p className="field">
          <label htmlFor="restrict-duration">{t("moderation.duration")}</label>
          <select
            id="restrict-duration"
            value={durationHours === null ? "" : String(durationHours)}
            onChange={(event) =>
              setDurationHours(event.target.value === "" ? null : Number(event.target.value))
            }
          >
            {DURATIONS.map((option) => (
              <option
                key={option.label}
                value={option.hours === null ? "" : String(option.hours)}
              >
                {t(option.label)}
              </option>
            ))}
          </select>
        </p>

        <p className="field">
          <label htmlFor="restrict-reasoning">{t("moderation.noteLabel")}</label>
          <textarea
            id="restrict-reasoning"
            required
            maxLength={500}
            rows={3}
            value={reasoning}
            aria-describedby="restrict-reasoning-hint"
            onChange={(event) => setReasoning(event.target.value)}
          />
          <span id="restrict-reasoning-hint" className="muted">
            {t("moderation.noteHint")}
          </span>
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={open === "restore"}
        title={t("moderation.restoreTitle")}
        description={t("moderation.restoreBody", { name: displayName })}
        confirmLabel={t("moderation.restore")}
        busy={busy}
        error={error}
        onCancel={close}
        onConfirm={() => void run(() => restoreAccount(userId))}
      />
    </>
  );
}

export { CATEGORY_LABELS };
