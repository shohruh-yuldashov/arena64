import { useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useRef } from "react";

import { usePendingMatch } from "@/features/matchmaking/model/queries";
import { useMatchOfferPush } from "@/features/matchmaking/model/use-match-offers";
import { MatchOfferDialog } from "@/features/matchmaking/ui/match-offer-dialog";

/**
 * The pending match offer, on every authenticated page — A64-022.6 §13.
 *
 * ## The gap this closes
 *
 * A64-020.5D shipped `useMatchOfferPush` mounted by the lobby and recorded
 * the limitation in writing: *"a player on `/profile` when they are paired
 * learns on their next visit to `/play`"*. A64-022.5 inherited it, and
 * friend challenges made it matter — a challenger who is told "your
 * challenge was accepted" while reading a profile now has a real game
 * waiting inside a ten-minute join window, and nothing on that page would
 * have told them.
 *
 * So the **existing** dialog moves here, and `/play` and `/challenges` stop
 * rendering their own. There is exactly one `MatchOfferDialog` in the app.
 *
 * ## What it deliberately does not do: navigate on `active` alone
 *
 * This is the part that had to be got right, and it is why §13's audit was
 * worth doing before writing anything.
 *
 * `GET /matchmaking/matches/pending` returns a match that is
 * `pending_acceptance` **or `active`**, with no time window —
 * `MatchRecordRepository.pending_for` says so explicitly, and it is correct
 * for the lobby, where the first acceptor needs to learn their game began.
 *
 * A shell that derived "there is an active match, go there" would therefore
 * navigate on **every authenticated page load for the whole duration of a
 * game**. A player could not open their profile mid-match; they would be
 * thrown back to the board. That is not a global handoff, it is a trap.
 *
 * So this navigates only for a match it **showed an offer for**. The rule
 * is one ref:
 *
 *     saw an offer for match X, X is now active   -> go to the board
 *     X was already active when this mounted      -> do nothing
 *
 * The first is the handoff; the second is a player who is already in a game
 * and reading something else. `/play` keeps its own `transitioning`
 * navigation for its documented reload recovery, and the two are guarded
 * independently — they target the same route, so a double dispatch is a
 * no-op rather than a conflict.
 *
 * ## Everything else it does **not** add
 *
 *   no second socket        `useMatchOfferPush` fans out over the one
 *                           connection `app/providers` owns
 *   no second query         the same `matchmakingKeys.pending()` entry the
 *                           lobby reads; TanStack serves one request to
 *                           however many observers exist
 *   no new polling          the interval is the query's own, and it is the
 *                           one it has always applied to an open offer —
 *                           ticket or no ticket
 *   nothing anonymous       `AppShell` renders this only for a signed-in
 *                           session, so a logged-out visitor makes no
 *                           request
 */
export function MatchOfferSurface() {
  const navigate = useNavigate();

  // The same wake-up subscription the lobby has always mounted, moved to
  // where the dialog now lives so "a pairing woke us up" and "here is the
  // dialog" are one component's concern.
  useMatchOfferPush();

  // `false`: this surface has no queue ticket to report. An **open offer**
  // keeps the fast interval regardless — see `usePendingMatch` — which is
  // exactly the case that matters here and is also why a player who is
  // merely in a game costs nothing.
  const pending = usePendingMatch(false);
  const match = pending.data ?? null;

  const offered = useRef<string | null>(null);

  /**
   * The match this surface has already handed off to — **not a boolean**.
   *
   * A64-022.7 found the defect a boolean caused. `PlayPage` guards its own
   * navigation with `useRef(false)` and that is correct *there*, because the
   * page unmounts on the way to the board and remounts with a fresh ref.
   * This surface is mounted by `AppShell`, which never unmounts: a `true`
   * set on the first match of a session would still be `true` for the
   * second, and every later handoff would be silently skipped.
   *
   * Keyed by id, the guard does what it was meant to — one navigation per
   * match — and it also survives an account switch, where a stale id from
   * the previous user can never equal a new one.
   */
  const handedOff = useRef<string | null>(null);

  const goToGame = useCallback(
    (matchId: string) => {
      if (handedOff.current === matchId) return;
      handedOff.current = matchId;
      void navigate({ to: "/games/$matchId", params: { matchId } });
    },
    [navigate],
  );

  const open = match !== null && match.status === "pending_acceptance";
  if (open) offered.current = match.match_id;

  // The handoff, and **only** for a match this surface offered. Run in an
  // effect rather than during render: navigating inside React's commit
  // re-renders this component, which navigates again — the infinite-update
  // loop `PlayPage` documents from A64-020.2.
  const ready =
    match !== null && match.status === "active" && offered.current === match.match_id
      ? match.match_id
      : null;
  useEffect(() => {
    if (ready !== null) goToGame(ready);
  }, [ready, goToGame]);

  if (!open || match === null) return null;

  return (
    <MatchOfferDialog
      // Keyed by the match, so a second offer after a decline mounts a
      // fresh dialog rather than reusing one whose countdown is running
      // against the previous deadline.
      key={match.match_id}
      match={match}
      onExpired={() => void pending.refetch()}
      onAccepted={goToGame}
    />
  );
}
