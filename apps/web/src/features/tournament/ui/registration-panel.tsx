import { useState } from "react";

import type { Registration, Tournament } from "@/features/tournament/api";
import { registrationErrorKey } from "@/features/tournament/model/errors";
import {
  useEnterTournament,
  useMyRegistration,
  useWithdrawFromTournament,
} from "@/features/tournament/model/queries";
import { useTranslation } from "@/shared/i18n";
import { useHoldAppUpdate } from "@/shared/pwa";
import {
  Button,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Skeleton,
  Spinner,
} from "@/shared/ui";

/**
 * Enter, leave, and what state the viewer is actually in — A64-020.6 §8–§11.
 *
 * ## The server decides, this renders
 *
 * `useMyRegistration` is the authority for "am I in this", and
 * `tournament.status` is the authority for "may anybody enter". Nothing
 * here infers either: §8 forbids deriving registration status from button
 * availability, and the inversion is not hypothetical — a page that decided
 * locally would let a player press Enter on a tournament that filled two
 * seconds ago and would then have to explain the `409` it caused.
 *
 * The client's *only* local judgement is which control to offer, and every
 * one of them is re-checked by the server under a row lock.
 *
 * ## The deadline is displayed, never enforced
 *
 * §11. A countdown that reached zero and disabled the button would be this
 * client deciding registration had closed, using a clock that is not the
 * server's. The deadline is rendered as a date and the button stays live;
 * if it has genuinely passed, the server answers
 * `registration_deadline_passed` and that is what the player is told.
 *
 * ## Withdrawal is confirmed, and the confirmation is honest
 *
 * §10. It says the seat is released and that re-entry is possible while
 * registration is open — both of which the backend actually does. It makes
 * no claim about a rating penalty, a reseed or a refund, because the
 * contract states none and inventing consequences to discourage an action
 * is worse than allowing it.
 */
export function RegistrationPanel({ tournament }: { tournament: Tournament }) {
  const { t } = useTranslation();
  const [confirming, setConfirming] = useState(false);

  const registration = useMyRegistration(tournament.id);
  const enter = useEnterTournament(tournament.id);
  const withdraw = useWithdrawFromTournament(tournament.id);

  // Registration is open **according to the server's status field**, which
  // is the same field the endpoint checks under its lock.
  const open = tournament.status === "registration_open";
  const entry: Registration | null | undefined = registration.data;
  const isRegistered = entry !== null && entry !== undefined && entry.status === "registered";
  const hasWithdrawn = entry !== null && entry !== undefined && entry.status === "withdrawn";

  const failure = enter.error ?? withdraw.error;
  const pending = enter.isPending || withdraw.isPending;

  // A64-020.9 §14. Entering and withdrawing are not idempotent and are
  // deliberately not retried (`shared/api/query-client.ts`), so a reload
  // while one is in flight leaves the player unable to tell whether they
  // are registered. The update waits until the server has answered.
  useHoldAppUpdate(pending);

  return (
    <section
      aria-labelledby="registration-heading"
      className="border-border flex flex-col gap-3 rounded-lg border p-4"
    >
      <h2 id="registration-heading" className="text-sm font-semibold">
        {t("tournament.registration.title")}
      </h2>

      {registration.isPending ? (
        <>
          <span role="status" className="sr-only">
            {t("tournament.registration.loading")}
          </span>
          <Skeleton className="h-5 w-40" />
        </>
      ) : (
        <p className="text-sm">
          {isRegistered && t("tournament.registration.registered")}
          {hasWithdrawn && t("tournament.registration.withdrawn")}
          {entry === null && t("tournament.registration.notRegistered")}
          {isRegistered && entry.seed_number != null && (
            <span className="text-muted-foreground ml-2 text-xs">
              {t("tournament.registration.seed", { seed: entry.seed_number })}
            </span>
          )}
        </p>
      )}

      {/* §22: no registration controls on a tournament nobody can enter,
          and a sentence saying so instead of a disabled button whose
          reason is invisible. */}
      {!open && !isRegistered && (
        <p className="text-muted-foreground text-sm">{t("tournament.registration.closed")}</p>
      )}

      {open && !isRegistered && (
        <Button
          className="min-h-11 self-start"
          // Disabled **while in flight only** — §9's "disable duplicate
          // submission". Not disabled on a rule this client believes,
          // which would be the local decision §8 forbids.
          disabled={pending}
          onClick={() => enter.mutate()}
        >
          {enter.isPending ? (
            <Spinner label={t("tournament.registration.registering")} />
          ) : (
            t("tournament.registration.register")
          )}
        </Button>
      )}

      {open && isRegistered && (
        <Button
          variant="outline"
          className="min-h-11 self-start"
          disabled={pending}
          onClick={() => setConfirming(true)}
        >
          {withdraw.isPending ? (
            <Spinner label={t("tournament.registration.withdrawing")} />
          ) : (
            t("tournament.registration.withdraw")
          )}
        </Button>
      )}

      {failure !== null && (
        <p role="alert" className="text-destructive text-sm">
          {t(registrationErrorKey(failure))}
        </p>
      )}

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("tournament.registration.confirmTitle")}</DialogTitle>
            <DialogDescription>{t("tournament.registration.confirmBody")}</DialogDescription>
          </DialogHeader>
          <div className="flex flex-wrap justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" className="min-h-11">
                {t("tournament.registration.cancel")}
              </Button>
            </DialogClose>
            <Button
              variant="destructive"
              className="min-h-11"
              disabled={pending}
              onClick={() => {
                setConfirming(false);
                withdraw.mutate();
              }}
            >
              {t("tournament.registration.confirmWithdraw")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </section>
  );
}
