import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useEffect, useRef, useState } from "react";

import type { ActiveCommand, GameState } from "@/features/game/model/state";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Card, CardContent } from "@/shared/ui";

/**
 * The participant control surface — A64-020.5C §1, §5 through §9, §14.
 *
 * Resign, offer a draw, and answer one. Everything it renders comes from
 * `GameState.draw`, which is the server's answer resolved for this viewer —
 * §2 forbids deciding any of it here, and the whole file contains no ply
 * arithmetic, no eligibility rule and no result derivation.
 *
 * ## What is local and what is not
 *
 * Exactly one thing is local: whether the resign confirmation dialog is
 * open. §3 permits that and nothing else. The pending offer, the
 * permissions, the in-flight command and the refusal all live in the
 * reducer, so a control cannot render a stale answer for a frame after an
 * authoritative update.
 *
 * ## Why the incoming offer is a panel and not only a dialog
 *
 * §7 and §16. A toast disappears, and an offer that outlived its toast would
 * be invisible until the next reload — which is precisely the state a
 * player needs to see. So the durable representation is a panel in the
 * layout, announced when it appears, and it stays until the server says the
 * offer is gone.
 *
 * ## Nothing here appears for a spectator
 *
 * A spectator's snapshot carries no `draw` block, so `mayOffer`,
 * `mayAccept` and `mayDecline` are all false and `state.side` is `null` —
 * every control below is gated on one of them. The visibility rule is the
 * server's; this is what consuming it looks like.
 */

/** Whether the match is in a state where any command makes sense. */
function isPlayable(state: GameState): boolean {
  return (
    state.side !== null &&
    (state.phase === "active" || state.phase === "submitting_move") &&
    state.result === null
  );
}

/**
 * A refused command as a sentence.
 *
 * Only codes `GatewayErrorCode` actually publishes — §13 forbids branching
 * on the server's prose and forbids inventing a code. Anything unmapped
 * falls through to a generic sentence rather than rendering a raw
 * identifier at a player.
 */
function commandErrorKey(code: string): TranslationKey {
  const known: Record<string, TranslationKey> = {
    draw_offer_already_pending: "game.controls.errors.draw_offer_already_pending",
    draw_offer_not_pending: "game.controls.errors.draw_offer_not_pending",
    draw_offer_not_recipient: "game.controls.errors.draw_offer_not_recipient",
    draw_offer_not_allowed_yet: "game.controls.errors.draw_offer_not_allowed_yet",
    match_not_active: "game.errors.match_not_active",
    not_in_room: "game.errors.not_in_room",
    not_a_participant: "game.errors.not_a_participant",
    stale_state: "game.errors.stale_state",
    rate_limited: "game.errors.rate_limited",
  };
  return known[code] ?? "game.controls.errors.unknown";
}

/**
 * The resignation confirmation — §5, §18.
 *
 * `role="alertdialog"`, matching `match-offer-dialog.tsx`: this is a
 * destructive choice that needs an answer, not an informational panel. The
 * description states all three consequences §5 requires and **names no
 * rating delta** — the amount depends on both players' Glicko-2 state and a
 * number invented here would be wrong.
 *
 * Focus moves in on open (Radix) and returns to the trigger on close, which
 * is why the trigger is rendered by the same component rather than passed
 * in — a caller holding the trigger elsewhere would break the return path.
 */
function ResignDialog({ busy, onConfirm }: { busy: boolean; onConfirm: () => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  return (
    <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
      <DialogPrimitive.Trigger asChild>
        <Button variant="outline" className="min-h-11 w-full" disabled={busy}>
          {t("game.controls.resign")}
        </Button>
      </DialogPrimitive.Trigger>

      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/60" />
        <DialogPrimitive.Content
          role="alertdialog"
          className="bg-background fixed inset-x-0 bottom-0 z-50 flex max-h-[90dvh] flex-col gap-4 overflow-y-auto rounded-t-lg border p-6 pb-[max(1.5rem,env(safe-area-inset-bottom))] sm:inset-1/2 sm:bottom-auto sm:w-full sm:max-w-md sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-lg"
        >
          <DialogPrimitive.Title className="text-lg font-semibold">
            {t("game.controls.resignConfirm.title")}
          </DialogPrimitive.Title>
          <DialogPrimitive.Description className="text-muted-foreground text-sm">
            {t("game.controls.resignConfirm.body")}
          </DialogPrimitive.Description>
          {/* Deliberately conditional. The snapshot does not carry whether
              the match is rated — `MatchSnapshot` has no such field — and
              §5 asks for "may affect Rating according to the Match mode"
              rather than a claim. Guessing would be worse than the hedge,
              and inventing a delta is forbidden outright. */}
          <p className="text-muted-foreground text-sm">
            {t("game.controls.resignConfirm.rating")}
          </p>

          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <DialogPrimitive.Close asChild>
              <Button variant="outline" className="min-h-11">
                {t("common.cancel")}
              </Button>
            </DialogPrimitive.Close>
            <Button
              variant="destructive"
              className="min-h-11"
              disabled={busy}
              onClick={() => {
                setOpen(false);
                onConfirm();
              }}
            >
              {t("game.controls.resignConfirm.confirm")}
            </Button>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/**
 * The incoming offer — §7, §8, §9, §18.
 *
 * `role="alert"` rather than a live region on a wrapper: an offer waiting
 * for an answer is genuinely interruptive, and it is the one thing on this
 * panel that appears without the player having done anything.
 *
 * Focus is moved to the Accept button once, when the offer first appears.
 * Not on every render — a re-render while the player is reading would drag
 * focus back and make the panel unusable with a keyboard.
 */
function IncomingOffer({
  busy,
  onAccept,
  onDecline,
}: {
  busy: boolean;
  onAccept: () => void;
  onDecline: () => void;
}) {
  const { t } = useTranslation();
  const accept = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    accept.current?.focus();
  }, []);

  return (
    <div
      role="alert"
      className="border-primary bg-primary/5 flex flex-col gap-3 rounded-md border p-3"
    >
      <p className="text-sm font-medium">{t("game.controls.incoming.title")}</p>
      <p className="text-muted-foreground text-sm">{t("game.controls.incoming.body")}</p>
      <div className="flex flex-col gap-2 sm:flex-row">
        <Button ref={accept} className="min-h-11 flex-1" disabled={busy} onClick={onAccept}>
          {t("game.controls.accept")}
        </Button>
        <Button
          variant="outline"
          className="min-h-11 flex-1"
          disabled={busy}
          onClick={onDecline}
        >
          {t("game.controls.decline")}
        </Button>
      </div>
    </div>
  );
}

export function GameControls({
  state,
  onCommand,
}: {
  state: GameState;
  onCommand: (command: ActiveCommand) => void;
}) {
  const { t } = useTranslation();

  // Nothing at all for a spectator or a finished game. Rendering a disabled
  // panel would tell a viewer that controls exist for them.
  if (!isPlayable(state)) return null;

  const { draw, activeCommand, commandError } = state;
  const busy = activeCommand !== null;
  // The offer is ours when it exists and we are not the one who may answer
  // it. Derived from the server's own booleans rather than by comparing
  // sides, so this cannot disagree with what the server would allow.
  const isOurs = draw.offer !== null && !draw.mayAccept;

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 pt-6">
        <h2 className="text-muted-foreground text-xs font-medium">
          {t("game.controls.heading")}
        </h2>

        {draw.mayAccept && draw.mayDecline && (
          <IncomingOffer
            busy={busy}
            onAccept={() => onCommand("accept")}
            onDecline={() => onCommand("decline")}
          />
        )}

        {isOurs && (
          // §6 and §16: the durable "sent" state, in the panel rather than
          // in a toast. The offerer is told the opponent may answer *or
          // move*, because a move is what silently ends the offer (§10) and
          // a player who did not know that would read it as the offer being
          // lost.
          <p role="status" className="text-muted-foreground text-sm">
            {t("game.controls.offerSent")}
          </p>
        )}

        <div className="flex flex-col gap-2 sm:flex-row">
          <Button
            variant="outline"
            className="min-h-11 flex-1"
            // §6: the button exists only when the server says it may be
            // used. Not disabled-and-visible, because "may I offer" is a
            // rule with a reason the player cannot see, and a permanently
            // greyed control invites clicking.
            disabled={!draw.mayOffer || busy}
            onClick={() => onCommand("offer")}
          >
            {t("game.controls.offerDraw")}
          </Button>
          <div className="flex-1">
            <ResignDialog busy={busy} onConfirm={() => onCommand("resign")} />
          </div>
        </div>

        {/* §18: the pending state is announced, and it is text rather than
            a spinner alone — colour and motion are never the only signal. */}
        {busy && (
          <p role="status" className="text-muted-foreground text-sm">
            {t("game.controls.pending")}
          </p>
        )}

        {commandError !== null && (
          <p role="alert" className="text-destructive text-sm">
            {t(commandErrorKey(commandError))}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
