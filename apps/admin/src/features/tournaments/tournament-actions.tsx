import { useState } from "react";

import {
  type AdminTournamentAction,
  commandTournament,
  type TournamentCommand,
} from "@/shared/api/client";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

/**
 * The lifecycle commands a tournament's current state allows — A64-024.5H.
 *
 * **Derived from the server's status, never from a local guess**, and the
 * backend validates independently: the aggregate refuses under a row lock,
 * so a button that should not have been rendered still cannot do anything.
 * What the derivation buys is that an operator is not offered a move that
 * will be refused.
 *
 * ## Only three commands, and no disabled fantasies
 *
 * There is no "publish round" and no "cancel". Round publication follows
 * from match results (`TournamentAdvancementService`), and cancellation has
 * no finished semantics in this repository — `specs/admin.md` §6.15 records
 * both. A greyed-out button for either would imply the platform has an
 * answer it is withholding.
 *
 * ## Start confirms; the other two do not
 *
 * Starting freezes the field, builds the bracket and creates real games for
 * real people — §18's bar for deliberate confirmation. Opening and closing
 * registration are reversible in effect (a closed tournament can still be
 * started, an open one closed) and reachable again from the other side, so
 * a dialog on each would be friction that teaches an operator to click
 * through dialogs.
 */

/** What each lifecycle state allows, as the server defines it. */
const COMMANDS: Record<string, { command: TournamentCommand; label: TranslationKey }[]> = {
  draft: [{ command: "registration/open", label: "tournamentActions.openRegistration" }],
  registration_open: [
    { command: "registration/close", label: "tournamentActions.closeRegistration" },
  ],
  registration_closed: [{ command: "start", label: "tournamentActions.start" }],
};

const DONE: Record<TournamentCommand, TranslationKey> = {
  "registration/open": "tournamentActions.doneOpened",
  "registration/close": "tournamentActions.doneClosed",
  start: "tournamentActions.doneStarted",
};

/**
 * What a `409` means, per command.
 *
 * The aggregate refuses a transition it is not in a state to make, and
 * which transition was asked for is the whole of what an operator needs to
 * know — "the action was refused" leaves them to work out which of the
 * three buttons they pressed. The server does not say more, deliberately
 * (the reason is a domain state, not a message), so the command is the only
 * thing that can distinguish them and it is on this side.
 */
const REFUSED: Record<TournamentCommand, TranslationKey> = {
  "registration/open": "tournamentActions.refusedOpen",
  "registration/close": "tournamentActions.refusedClose",
  start: "tournamentActions.refusedStart",
};

export function TournamentActions({
  tournamentId,
  name,
  status,
  onChanged,
}: {
  tournamentId: string;
  name: string;
  status: string;
  onChanged: (result: AdminTournamentAction) => void;
}) {
  const { t } = useTranslation();

  const [pending, setPending] = useState<TournamentCommand | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<TranslationKey | null>(null);

  const available = COMMANDS[status] ?? [];

  const run = async (command: TournamentCommand) => {
    // The disabled attribute is UX; this is the guard. A double click, a
    // held `Enter`, or a click landing between the request and the re-read
    // would otherwise send the command twice — harmless against the
    // aggregate's row lock, and still two audit entries where one action
    // happened.
    if (busy) return;
    setBusy(true);
    setError(null);
    const outcome = await commandTournament(tournamentId, command);
    setBusy(false);

    if (outcome.status === "ok") {
      onChanged(outcome.value);
      setNotice(DONE[command]);
      setPending(null);
      return;
    }
    setError(t(outcome.status === "refused" ? REFUSED[command] : "tournamentActions.failed"));
  };

  if (available.length === 0) {
    return (
      <>
        <p role="status">{t("tournamentActions.noActions")}</p>
        <p className="muted">{t("tournamentActions.noActionsHint")}</p>
      </>
    );
  }

  return (
    <>
      <p className="tournament-actions">
        {available.map(({ command, label }) =>
          command === "start" ? (
            <button
              key={command}
              type="button"
              className="action danger"
              disabled={busy}
              onClick={() => {
                setError(null);
                setPending(command);
              }}
            >
              {t(label)}
            </button>
          ) : (
            <button
              key={command}
              type="button"
              className="action"
              disabled={busy}
              onClick={() => void run(command)}
            >
              {t(label)}
            </button>
          ),
        )}
      </p>

      {notice !== null && (
        <p role="status" className="notice">
          {t(notice)}
        </p>
      )}
      {error !== null && pending === null && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      <ConfirmDialog
        open={pending === "start"}
        title={t("tournamentActions.confirmStartTitle")}
        description={t("tournamentActions.confirmStartBody", { name })}
        confirmLabel={t("tournamentActions.start")}
        busy={busy}
        error={pending === "start" ? error : null}
        onCancel={() => setPending(null)}
        onConfirm={() => void run("start")}
      >
        <p className="muted">{t("tournamentActions.confirmStartHint")}</p>
      </ConfirmDialog>
    </>
  );
}
