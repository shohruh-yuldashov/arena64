import { useState } from "react";

import type { RelationshipState } from "@/entities/relationship";
import { CreateChallengeDialog } from "@/features/challenges/ui/create-challenge-dialog";
import { useTranslation } from "@/shared/i18n";
import { Button } from "@/shared/ui";

/**
 * "Challenge" — the one entry point, wherever a friend appears.
 * A64-022.5 §4, §13, §14.
 *
 * Used by the friends list and the public profile. Duplicating the button
 * and its dialog per surface is how the two end up offering different
 * clocks: two copies of a flow agree until one is edited.
 *
 * ## When it renders, and who decides
 *
 * `state === "friends"` and nothing else. §13 lists the cases it must hide
 * for — self, blocked, not friends — and all three are the same condition
 * read from one value: `RelationshipState` is a closed enum the **server**
 * computes and puts on the profile it returns, so this component answers
 * "may I challenge them" by asking whether they are a friend, which is what
 * the backend requires.
 *
 * Self is covered because a profile of oneself carries no `relationship` at
 * all — the API omits it — and blocked is covered because a block is not
 * friendship. Neither needs a branch here.
 *
 * ## What it deliberately does **not** do
 *
 * §13's fourth case is "already pending challenge", and this button does
 * not hide for it. Knowing would cost a read of the outgoing list on every
 * surface that renders a friend — an N+1 in the shape §20 forbids — and it
 * would still be a cached answer racing the create.
 *
 * So a second challenge to the same person is **allowed to be attempted**
 * and is refused by `uq_friend_challenge__live_pair`, which the dialog
 * renders as "you already have a live challenge with them". That is the
 * honest division: the server owns the rule, and the client stops guessing
 * at it. The `/challenges` page is where an outstanding invitation is
 * actually visible, and it is one tap away.
 */
export function ChallengeButton({
  playerId,
  playerName,
  state,
  size = "sm",
}: {
  playerId: string;
  playerName: string;
  /**
   * The server's own answer. Anything but `friend` renders nothing.
   *
   * Typed as the generated union rather than `string`, which is not
   * pedantry: the first version of this compared against `"friends"` — a
   * value the enum does not have — and rendered the button for nobody. A
   * closed type turns that into a compile error.
   */
  state: RelationshipState | null | undefined;
  size?: "sm" | "default";
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  if (state !== "friend") return null;

  return (
    <>
      <Button
        variant="outline"
        size={size}
        className="min-h-11"
        aria-label={t("challenges.actions.challengeLabel", { name: playerName })}
        onClick={() => setOpen(true)}
      >
        {t("challenges.actions.challenge")}
      </Button>

      {/* Mounted only while open, so a list of forty friends holds one
          dialog rather than forty — and so the catalogue query inside it is
          not started forty times on a page nobody has clicked. */}
      {open && (
        <CreateChallengeDialog
          open={open}
          onOpenChange={setOpen}
          opponentId={playerId}
          opponentName={playerName}
        />
      )}
    </>
  );
}
