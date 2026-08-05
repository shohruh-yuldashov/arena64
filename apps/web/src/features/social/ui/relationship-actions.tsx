import { useState } from "react";

import {
  actionsFor,
  isDestructive,
  type RelationshipAction,
  type RelationshipState,
} from "@/entities/relationship";
import { FormError } from "@/features/auth/ui/form-status";
import { socialErrorKey } from "@/features/social/model/error-messages";
import {
  useAcceptRequest,
  useBlockPlayer,
  useCancelRequest,
  useDeclineRequest,
  useRemoveFriend,
  useSendRequest,
  useUnblockPlayer,
} from "@/features/social/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import {
  Button,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Spinner,
} from "@/shared/ui";

/**
 * The one place a social transition is written — A64-020.4 §6.
 *
 * Used by search rows, the friends list, both request lists and the public
 * profile. Duplicating the transitions per page is how "Add friend" ends up
 * beside "Accept" on one of them: five copies of a rule agree until one is
 * edited.
 *
 * ## The button set comes from one value
 *
 * `actionsFor(state)` maps a single closed enum to a list. There is no
 * arrangement of props that produces a contradictory pair, because there is
 * only one input — the impossible combinations §6 forbids are not filtered
 * out here, they are unrepresentable.
 *
 * ## Identity is the server's
 *
 * `playerId` and `requestId` come from a response this client received.
 * Nothing here accepts a username typed by a person, and the *sender* is
 * never sent at all — the API takes it from the access token.
 *
 * `requestId` is required for the two request transitions and absent
 * elsewhere, which is why it is optional: a search row knows the state is
 * `incoming_request` but not which row carries it, so it renders no accept
 * button. That is a real limitation of the search response and is stated
 * rather than worked around with a lookup.
 */
export function RelationshipActions({
  playerId,
  playerName,
  state,
  requestId,
  size = "sm",
}: {
  playerId: string;
  /** Named in the confirmation, so a destructive click cannot be ambiguous. */
  playerName: string;
  state: RelationshipState | null | undefined;
  requestId?: string | undefined;
  size?: "sm" | "default";
}) {
  const { t } = useTranslation();
  const [failure, setFailure] = useState<TranslationKey | null>(null);
  const [confirming, setConfirming] = useState<RelationshipAction | null>(null);

  const send = useSendRequest();
  const cancel = useCancelRequest();
  const accept = useAcceptRequest();
  const decline = useDeclineRequest();
  const remove = useRemoveFriend();
  const block = useBlockPlayer();
  const unblock = useUnblockPlayer();

  const pending =
    send.isPending ||
    cancel.isPending ||
    accept.isPending ||
    decline.isPending ||
    remove.isPending ||
    block.isPending ||
    unblock.isPending;

  async function run(action: RelationshipAction): Promise<void> {
    setFailure(null);
    try {
      switch (action) {
        case "send_request":
          await send.mutateAsync(playerId);
          break;
        case "cancel_request":
          if (requestId !== undefined) await cancel.mutateAsync(requestId);
          break;
        case "accept_request":
          if (requestId !== undefined) await accept.mutateAsync(requestId);
          break;
        case "decline_request":
          if (requestId !== undefined) await decline.mutateAsync(requestId);
          break;
        case "remove_friend":
          await remove.mutateAsync(playerId);
          break;
        case "block":
          await block.mutateAsync(playerId);
          break;
        case "unblock":
          await unblock.mutateAsync(playerId);
          break;
      }
      setConfirming(null);
    } catch (error) {
      setFailure(socialErrorKey(error));
      setConfirming(null);
    }
  }

  // A search row cannot act on a request it has no id for. Hiding the
  // button is the honest answer — a disabled one that looked available
  // would be worse — and the row still links to the profile, where the
  // request lists provide the id.
  const available = actionsFor(state).filter(
    (action) =>
      requestId !== undefined ||
      !["cancel_request", "accept_request", "decline_request"].includes(action),
  );

  if (available.length === 0 && failure === null) return null;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        {available.map((action) => (
          <Button
            key={action}
            size={size}
            variant={action === "accept_request" ? "default" : "outline"}
            className="min-h-11"
            disabled={pending}
            // The player's name is in the accessible name, so a screen
            // reader hears "Add friend, Ali" rather than twelve identical
            // "Add friend" buttons down a list.
            aria-label={`${t(LABELS[action])} — ${playerName}`}
            onClick={() => (isDestructive(action) ? setConfirming(action) : void run(action))}
          >
            {pending ? <Spinner label={t("social.actions.working")} /> : t(LABELS[action])}
          </Button>
        ))}
      </div>

      {failure !== null && <FormError messageKey={failure} />}

      <Dialog open={confirming !== null} onOpenChange={(open) => !open && setConfirming(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {confirming !== null ? t(CONFIRM_TITLES[confirming]) : ""}
            </DialogTitle>
            {/* The consequence, and only what the backend actually
                guarantees. No claim that messages are deleted or games
                cancelled — nothing in the contract says so. */}
            <DialogDescription>
              {confirming !== null ? t(CONFIRM_BODIES[confirming], { name: playerName }) : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-wrap justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" className="min-h-11">
                {t("social.actions.cancelDialog")}
              </Button>
            </DialogClose>
            <Button
              variant="destructive"
              className="min-h-11"
              disabled={pending}
              onClick={() => confirming !== null && void run(confirming)}
            >
              {pending ? (
                <Spinner label={t("social.actions.working")} />
              ) : (
                t("social.actions.confirm")
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

const LABELS: Record<RelationshipAction, TranslationKey> = {
  send_request: "social.actions.sendRequest",
  cancel_request: "social.actions.cancelRequest",
  accept_request: "social.actions.accept",
  decline_request: "social.actions.decline",
  remove_friend: "social.actions.removeFriend",
  block: "social.actions.block",
  unblock: "social.actions.unblock",
};

const CONFIRM_TITLES: Record<RelationshipAction, TranslationKey> = {
  send_request: "social.actions.sendRequest",
  cancel_request: "social.actions.cancelRequest",
  accept_request: "social.actions.accept",
  decline_request: "social.actions.decline",
  remove_friend: "social.confirm.removeTitle",
  block: "social.confirm.blockTitle",
  unblock: "social.actions.unblock",
};

const CONFIRM_BODIES: Record<RelationshipAction, TranslationKey> = {
  send_request: "social.actions.sendRequest",
  cancel_request: "social.actions.cancelRequest",
  accept_request: "social.actions.accept",
  decline_request: "social.actions.decline",
  remove_friend: "social.confirm.removeBody",
  block: "social.confirm.blockBody",
  unblock: "social.actions.unblock",
};
