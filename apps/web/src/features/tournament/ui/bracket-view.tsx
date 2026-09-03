import { Link } from "@tanstack/react-router";

import type { Bracket, BracketNode, TournamentParticipant } from "@/features/tournament/api";
import {
  liveMatchOf,
  type NodeState,
  nodeStateOf,
  participantsById,
  replayableMatchesOf,
} from "@/features/tournament/model/bracket";
import { roundNameKey, roundStatusKey } from "@/features/tournament/ui/labels";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";

/**
 * A single-elimination bracket — A64-020.6 §13, §14, §24, §25.
 *
 * ## Rounds are columns, and the columns scroll
 *
 * §25 offers horizontal scroll or a round-at-a-time segmented view; this
 * takes the first. A player comparing "who is in the semi-final" against
 * "who they beat" wants both columns visible, and a segmented view hides
 * exactly that relationship behind a control. The scroll container is the
 * **only** thing on the page that scrolls sideways — the page body never
 * does (§25) — and it is labelled and focusable so a keyboard user can
 * reach later rounds without a pointer (§24).
 *
 * A 128-player bracket is seven columns. It is wide, and it is *meant* to
 * be wide: §13 forbids squeezing one into microscopic cards, and a
 * scrollable column of readable rows beats an unreadable diagram.
 *
 * ## No canvas, no zoom — and, since A64-025.7, connectors
 *
 * §13 rules out drag and zoom, and both stay ruled out.
 *
 * Connectors were left out with them, on the argument that drawing an edge
 * means absolute positioning and fixed row heights, and that fixed heights
 * are what stop a bracket reflowing at 360px. The first half was right and
 * the conclusion was not: the edges here are drawn from **equal-flex
 * columns**, so a node in round N occupies exactly the height of the two it
 * feeds from in round N-1 and its centre is their midpoint by construction.
 * No height is stated anywhere, and the bracket reflows exactly as it did.
 *
 * **Every edge is the domain relationship, never a guess.**
 * `BracketSlot.parent()` is `(round + 1, slot // 2)` and
 * `takes_light_seat_of_parent()` is `slot % 2 == 0` — so an even slot draws
 * its line downward to the pair's midpoint and an odd one upward, and the
 * two meet at the height of the node they feed. Nothing measures a
 * rendered box to decide where a line goes.
 *
 * The lines are `aria-hidden`: a connector is invisible to a screen reader,
 * which is why the round heading and the slot number still carry the
 * relationship in text. The drawing is an addition to that, not a
 * replacement for it.
 *
 * ## Five node states, five renderings
 *
 * The whole point of §13's list. `bye` and `pending` both show one name and
 * one blank, and they mean opposite things: one is decided, the other is
 * waiting. Each is labelled in words.
 */
export function BracketView({ bracket }: { bracket: Bracket }) {
  const { t } = useTranslation();
  const participants = participantsById(bracket.participants ?? []);
  const totalRounds = bracket.rounds.length;

  if (bracket.rounds.length === 0) {
    return <p className="text-muted-foreground text-sm">{t("tournament.bracket.empty")}</p>;
  }

  return (
    <div className="flex flex-col gap-2">
      <p className="text-muted-foreground text-xs sm:hidden">
        {t("tournament.bracket.scrollHint")}
      </p>

      {/* `tabIndex` makes the scroller reachable by keyboard: a region that
          scrolls but cannot be focused is unreachable without a pointer,
          which is WCAG 2.1.1. `role="region"` plus the label is what names
          it in the landmark list.

          Labelled "Bracket rounds" rather than "Bracket", because the
          `<section>` this sits inside is already a region named after the
          heading — two landmarks with one name are indistinguishable in a
          landmark list, which is worse than one. */}
      {/* `gap-8` is load-bearing rather than taste: it is twice `CONNECTOR`,
          so a child's outgoing stub and its parent's incoming one meet in
          the middle of the gap, where the vertical line stands. */}
      <div
        role="region"
        aria-label={t("tournament.bracket.rounds")}
        tabIndex={0}
        className="focus-visible:ring-ring flex items-stretch gap-8 overflow-x-auto pb-2 focus-visible:ring-2 focus-visible:outline-none"
      >
        {bracket.rounds.map((round, roundIndex) => {
          const named = roundNameKey(round.round_number, totalRounds);
          const heading = named
            ? t(named)
            : t("tournament.bracket.round", { round: round.round_number });

          return (
            <section
              key={round.round_number}
              aria-label={heading}
              className="flex min-w-[15rem] flex-col gap-2 sm:min-w-[17rem]"
            >
              <header className="flex flex-col">
                <h3 className="text-sm font-semibold">{heading}</h3>
                <span className="text-muted-foreground text-xs">
                  {t(roundStatusKey(round.status))}
                </span>
              </header>

              {/* `flex-1` on the list and on every node is the whole
                  geometry: each round divides the same column height by its
                  own node count, so one node in round N is exactly as tall
                  as the two in round N-1 that feed it, and their centres
                  meet without a single stated height. */}
              <ol className="flex flex-1 flex-col">
                {round.nodes.map((node) => (
                  <BracketNodeCard
                    key={node.pairing_id}
                    node={node}
                    participants={participants}
                    hasParent={roundIndex < bracket.rounds.length - 1}
                    hasChildren={roundIndex > 0}
                  />
                ))}
              </ol>
            </section>
          );
        })}
      </div>
    </div>
  );
}

const STATE_LABEL: Record<NodeState, TranslationKey> = {
  bye: "tournament.bracket.node.bye",
  completed: "tournament.bracket.node.completed",
  live: "tournament.bracket.node.live",
  ready: "tournament.bracket.node.ready",
  pending: "tournament.bracket.node.pending",
};

function BracketNodeCard({
  node,
  participants,
  hasParent,
  hasChildren,
}: {
  node: BracketNode;
  participants: Map<string, TournamentParticipant>;
  /** Whether a later round exists for this node to feed. */
  hasParent: boolean;
  /** Whether an earlier round exists for this node to be fed by. */
  hasChildren: boolean;
}) {
  const { t } = useTranslation();
  const state = nodeStateOf(node);

  const light = nameOf(node.light_player_id, participants, t);
  const dark = nameOf(node.dark_player_id, participants, t);
  const liveMatch = liveMatchOf(node);
  const replays = replayableMatchesOf(node);
  // `BracketSlot.takes_light_seat_of_parent()`, and the only thing the
  // drawing branches on: an even slot is the upper of the pair, so its line
  // runs down to their shared midpoint; an odd slot's runs up.
  const takesLightSeatOfParent = node.slot % 2 === 0;

  return (
    <li className="relative flex flex-1 items-center py-1">
      {hasParent && (
        <>
          {/* Out of the card, to where the pair's line stands. */}
          <span
            aria-hidden="true"
            className="border-border absolute top-1/2 -right-4 w-4 border-t"
          />
          {/* Down to the midpoint, or up to it. Half this node's height is
              exactly the distance to it, because the pair shares one
              parent's worth of column. */}
          <span
            aria-hidden="true"
            className={cn(
              "border-border absolute -right-4 h-1/2 border-l",
              takesLightSeatOfParent ? "top-1/2" : "bottom-1/2",
            )}
          />
        </>
      )}

      {hasChildren && (
        /* In from the pair's line to this card. */
        <span
          aria-hidden="true"
          className="border-border absolute top-1/2 -left-4 w-4 border-t"
        />
      )}

      <div className="border-border bg-card relative flex w-full flex-col gap-1 rounded-md border p-3">
        {/* The state in words, **once**. §24: a bracket must be understandable
          without colour, so the word carries the meaning and the colour only
          reinforces it for the one state worth drawing an eye to. A second
          badge beside it said "Being played" twice, which a screen reader
          reads twice and which says nothing the label had not. */}
        <span
          className={cn(
            "text-xs",
            state === "live" ? "text-primary font-semibold" : "text-muted-foreground",
          )}
        >
          {t(STATE_LABEL[state])}
        </span>

        <Seat
          playerId={node.light_player_id}
          name={light}
          seed={node.light_seed}
          isWinner={node.winner_id != null && node.winner_id === node.light_player_id}
        />
        <Seat
          playerId={node.dark_player_id}
          name={dark}
          seed={node.dark_seed}
          isWinner={node.winner_id != null && node.winner_id === node.dark_player_id}
        />

        {/* A bye is stated as a sentence, not implied by a blank seat. */}
        {state === "bye" && node.winner_id != null && (
          <p className="text-muted-foreground text-xs">
            {t("tournament.bracket.node.byeExplained", {
              player: nameOf(node.winner_id, participants, t),
            })}
          </p>
        )}

        {node.advancement_reason === "adjudication" && (
          <p className="text-muted-foreground text-xs">
            {t("tournament.bracket.advancedByAdjudication")}
          </p>
        )}

        {/* §15: a live match links to the game, a finished one to its
          replay, and a node with neither offers no link at all — a
          pending future node is not clickable. */}
        {liveMatch !== null && (
          <Link
            to="/games/$matchId"
            params={{ matchId: liveMatch }}
            aria-label={t("tournament.bracket.watchOf", { light, dark })}
            className="text-primary min-h-11 self-start px-1 py-2 text-sm underline-offset-4 hover:underline focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none"
          >
            {t("tournament.bracket.watch")}
          </Link>
        )}

        {replays.length > 0 && (
          <div className="flex flex-wrap gap-x-3">
            {replays.map((attempt) => (
              <Link
                key={attempt.match_id}
                to="/games/$matchId/replay"
                params={{ matchId: attempt.match_id }}
                aria-label={
                  replays.length > 1
                    ? t("tournament.bracket.replayNumbered", { number: attempt.attempt_number })
                    : t("tournament.bracket.replayOf", { light, dark })
                }
                className="text-primary min-h-11 px-1 py-2 text-sm underline-offset-4 hover:underline focus-visible:ring-ring focus-visible:ring-2 focus-visible:outline-none"
              >
                {replays.length > 1
                  ? `${t("tournament.bracket.replay")} ${attempt.attempt_number}`
                  : t("tournament.bracket.replay")}
              </Link>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

/**
 * One seat.
 *
 * An **empty** seat renders "to be decided" rather than a placeholder
 * player: §13 forbids a fake participant for a bye, and the same rule
 * applies to a slot still waiting for one. A blank would read as a name
 * that failed to load.
 *
 * The winner is marked with a word as well as weight, for §24.
 */
function Seat({
  playerId,
  name,
  seed,
  isWinner,
}: {
  playerId: string | null | undefined;
  name: string;
  seed: number | null | undefined;
  isWinner: boolean;
}) {
  const { t } = useTranslation();

  if (playerId == null) {
    return (
      <p className="text-muted-foreground text-sm italic">
        {t("tournament.bracket.node.empty")}
      </p>
    );
  }

  return (
    <p
      className={cn(
        "flex items-baseline justify-between gap-2 text-sm",
        isWinner && "font-semibold",
      )}
    >
      <span className="min-w-0 truncate">
        {name}
        {isWinner && (
          <span className="text-primary ml-1 text-xs font-medium">
            ({t("tournament.winner")})
          </span>
        )}
      </span>
      {seed != null && (
        <span className="text-muted-foreground shrink-0 text-xs tabular-nums">
          {t("tournament.bracket.seedOf", { seed })}
        </span>
      )}
    </p>
  );
}

/** A seat id as a name, from the batch the response already carried. */
function nameOf(
  playerId: string | null | undefined,
  participants: Map<string, TournamentParticipant>,
  t: (key: TranslationKey, values?: Record<string, string | number>) => string,
): string {
  if (playerId == null) return t("tournament.bracket.node.empty");
  const participant = participants.get(playerId);
  return participant?.display_name ?? participant?.username ?? t("tournament.unknownPlayer");
}
