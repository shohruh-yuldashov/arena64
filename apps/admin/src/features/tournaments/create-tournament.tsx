import { useState } from "react";

import { type AdminTournamentAction, createTournament } from "@/shared/api/client";
import { useTranslation } from "@/shared/i18n";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

/**
 * Creating a tournament — A64-024.5H §16.
 *
 * A dialog rather than a wizard: a tournament is six fields, and the
 * complexity a wizard exists to break up is not here. It reuses the same
 * `<dialog>` primitive the moderation and notification confirmations use,
 * so the focus trap, `Escape` and page inertness are the browser's.
 *
 * ## Only real configuration
 *
 * Six fields, and every one of them is a column the aggregate reads. There
 * is no id, no status, no creator and no format:
 *
 * - the first two are the server's,
 * - the creator is the signed-in administrator, and a field for it would
 *   let a client erase the distinction between "the platform created this"
 *   and "a named person did",
 * - and v0.x runs one format, so a selector would offer one option.
 *
 * The bounds on capacity are the aggregate's own. Stating them here is a
 * courtesy to the operator, not the guarantee — `Tournament.__post_init__`
 * refuses a value outside them whatever this form allows.
 */

const VARIANTS = ["russian_8x8"] as const;
const SPEEDS = ["bullet", "blitz", "rapid", "classical"] as const;

const MIN_CAPACITY = 2;
const MAX_CAPACITY = 128;

export function CreateTournament({
  onCreated,
}: {
  onCreated: (created: AdminTournamentAction) => void;
}) {
  const { t } = useTranslation();

  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [variant, setVariant] = useState<string>(VARIANTS[0]);
  const [speedClass, setSpeedClass] = useState<string>("blitz");
  const [capacity, setCapacity] = useState(8);
  const [rated, setRated] = useState(true);
  const [deadline, setDeadline] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    setOpen(false);
    setError(null);
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    const outcome = await createTournament({
      name,
      variant,
      speed_class: speedClass,
      capacity,
      rated,
      // A local `datetime-local` value carries no zone; sending it as-is
      // would be an instant the server reads in its own. Converted here so
      // the operator's clock and the stored deadline mean the same moment.
      ...(deadline ? { registration_deadline: new Date(deadline).toISOString() } : {}),
    });
    setBusy(false);

    if (outcome.status === "ok") {
      onCreated(outcome.value);
      setName("");
      setDeadline("");
      close();
      return;
    }
    setError(
      t(
        outcome.status === "refused" ? "tournamentActions.refused" : "tournamentActions.failed",
      ),
    );
  };

  return (
    <>
      <p className="tournament-actions">
        <button type="button" className="action" onClick={() => setOpen(true)}>
          {t("tournamentActions.create")}
        </button>
      </p>

      <ConfirmDialog
        open={open}
        title={t("tournamentActions.createTitle")}
        description={t("tournamentActions.deadlineHint")}
        confirmLabel={t("tournamentActions.createSubmit")}
        busy={busy}
        error={error}
        onCancel={close}
        onConfirm={() => void submit()}
      >
        <p className="field">
          <label htmlFor="tournament-name">{t("tournamentActions.fieldName")}</label>
          <input
            id="tournament-name"
            required
            maxLength={120}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </p>

        <p className="field">
          <label htmlFor="tournament-variant">{t("tournamentActions.fieldVariant")}</label>
          <select
            id="tournament-variant"
            value={variant}
            onChange={(event) => setVariant(event.target.value)}
          >
            {VARIANTS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </p>

        <p className="field">
          <label htmlFor="tournament-speed">{t("tournamentActions.fieldSpeed")}</label>
          <select
            id="tournament-speed"
            value={speedClass}
            onChange={(event) => setSpeedClass(event.target.value)}
          >
            {SPEEDS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </p>

        <p className="field">
          <label htmlFor="tournament-capacity">{t("tournamentActions.fieldCapacity")}</label>
          <input
            id="tournament-capacity"
            type="number"
            required
            min={MIN_CAPACITY}
            max={MAX_CAPACITY}
            value={capacity}
            onChange={(event) => setCapacity(Number(event.target.value))}
          />
        </p>

        <p className="field">
          <label htmlFor="tournament-rated">{t("tournamentActions.fieldRated")}</label>
          <select
            id="tournament-rated"
            value={rated ? "rated" : "casual"}
            onChange={(event) => setRated(event.target.value === "rated")}
          >
            <option value="rated">{t("tournamentActions.ratedYes")}</option>
            <option value="casual">{t("tournamentActions.ratedNo")}</option>
          </select>
        </p>

        <p className="field">
          <label htmlFor="tournament-deadline">{t("tournamentActions.fieldDeadline")}</label>
          <input
            id="tournament-deadline"
            type="datetime-local"
            value={deadline}
            onChange={(event) => setDeadline(event.target.value)}
          />
        </p>
      </ConfirmDialog>
    </>
  );
}
