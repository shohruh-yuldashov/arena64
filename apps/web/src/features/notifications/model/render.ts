import type { Notification } from "@/features/notifications/api";
import type { TranslationKey } from "@/shared/i18n";

/**
 * Turning one notification into the two things a row renders — A64-021.4 §20.
 *
 * ## The backend states facts; this chooses the sentence
 *
 * Nothing here reads a server-composed string. A notification arrives as a
 * `type` plus one typed subject — an actor, a tournament, or a game result —
 * and the functions below pick a **translation key** and the values to
 * interpolate into it. That is what makes the same row readable in uz, ru
 * and en, and it is why no server string can reach the DOM as markup.
 *
 * ## Exactly one subject key is populated, and the type says which
 *
 * `actor`, `tournament` and `game` are three nullable keys on the response.
 * A client could switch on which one is present; it switches on `type`
 * instead, and reads the key that type promises. The difference matters when
 * a backend adds a fourth: switching on presence would silently render the
 * wrong branch, where switching on type falls through to the generic
 * sentence — which is what §20's "unknown future type must render safely"
 * asks for.
 *
 * ## Why `game_completed` is three keys and not one
 *
 * "You won", "you lost" and "you drew" are different sentences in every
 * language this product ships, and in uz and ru they are not one string with
 * a substituted word. The backend already resolved the outcome from the
 * recipient's point of view, so this only has to choose.
 */

/** What a row says, as a key and the values to interpolate. */
export interface NotificationMessage {
  key: TranslationKey;
  values: Record<string, string | number>;
}

export function notificationMessage(notification: Notification): NotificationMessage {
  switch (notification.type) {
    case "friend_request_received":
    case "friend_request_accepted":
      return {
        key:
          notification.type === "friend_request_received"
            ? "notifications.types.friend_request_received"
            : "notifications.types.friend_request_accepted",
        values: { actor: actorNameOf(notification) },
      };

    case "tournament_registration_confirmed":
      return {
        key: "notifications.types.tournament_registration_confirmed",
        values: { tournament: notification.tournament?.tournament_name ?? "" },
      };

    case "tournament_round_published":
      return {
        key: "notifications.types.tournament_round_published",
        values: {
          tournament: notification.tournament?.tournament_name ?? "",
          // `?? 0` is unreachable for this type — the backend always sends a
          // round number with it — and is written rather than asserted
          // because a non-null assertion would be a claim this file cannot
          // check against a server it does not control.
          round: notification.tournament?.round_number ?? 0,
        },
      };

    case "tournament_completed": {
      const rank = notification.tournament?.final_rank ?? null;
      return {
        // A player with no recorded standing gets the shorter sentence
        // rather than "you finished null": they were in the tournament and
        // it ended, which is true, and inventing a placement is not.
        key:
          rank === null
            ? "notifications.types.tournament_completed"
            : "notifications.types.tournament_completed_ranked",
        values: {
          tournament: notification.tournament?.tournament_name ?? "",
          rank: rank ?? 0,
        },
      };
    }

    case "game_completed": {
      const opponent = opponentNameOf(notification);
      return {
        key: gameOutcomeKey(notification.game?.outcome, { named: opponent !== "" }),
        values: { opponent },
      };
    }

    default:
      return { key: "notifications.types.unknown", values: {} };
  }
}

/**
 * The avatar a row shows: a person where there is one, initials otherwise.
 *
 * `label` is what the fallback renders and what the accessible name is built
 * from, so a tournament notification is never announced as an unnamed image.
 * `thumbnailUrl` is `null` whenever there is no person — a tournament has no
 * picture, and inventing a placeholder image would be a second thing to
 * localise and cache.
 */
export interface NotificationSubject {
  label: string;
  thumbnailUrl: string | null;
}

export function notificationSubject(notification: Notification): NotificationSubject {
  if (notification.tournament) {
    return { label: notification.tournament.tournament_name, thumbnailUrl: null };
  }
  if (notification.game) {
    return {
      label: opponentNameOf(notification),
      thumbnailUrl: notification.game.opponent?.thumbnail_url ?? null,
    };
  }
  return {
    label: actorNameOf(notification),
    thumbnailUrl: notification.actor?.thumbnail_url ?? null,
  };
}

/**
 * The same fallback `entities/user.displayNameOf` applies, spelled out
 * because that helper takes a whole `UserRead` and these are snapshots of
 * three fields — widening it to accept both would make it accept anything
 * with a `username`.
 */
function actorNameOf(notification: Notification): string {
  const actor = notification.actor;
  return actor === null || actor === undefined ? "" : (actor.display_name ?? actor.username);
}

function opponentNameOf(notification: Notification): string {
  const opponent = notification.game?.opponent;
  return opponent === null || opponent === undefined
    ? ""
    : (opponent.display_name ?? opponent.username);
}

/**
 * A closed mapping over two real dimensions: what happened, and whether
 * there is still a name to say it against.
 *
 * The second is not defensive padding. An opponent whose account is gone
 * arrives as `null` — the backend keeps the game and loses the name — and
 * "You beat " is a sentence no language recovers from. Six keys is the cost
 * of never rendering one.
 *
 * The final fallback is a genuinely different case: an outcome this build
 * does not recognise, which means a backend that moved ahead of it. The
 * neutral sentence is true whatever the value turns out to mean.
 */
function gameOutcomeKey(
  outcome: string | undefined,
  { named }: { named: boolean },
): TranslationKey {
  switch (outcome) {
    case "win":
      return named
        ? "notifications.types.game_completed_win"
        : "notifications.types.game_completed_win_anonymous";
    case "loss":
      return named
        ? "notifications.types.game_completed_loss"
        : "notifications.types.game_completed_loss_anonymous";
    case "draw":
      return named
        ? "notifications.types.game_completed_draw"
        : "notifications.types.game_completed_draw_anonymous";
    default:
      return "notifications.types.game_completed";
  }
}
