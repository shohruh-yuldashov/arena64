import type { Notification } from "@/features/notifications/api";
import type { TranslationKey } from "@/shared/i18n";

/**
 * Turning one notification into the two things a row renders — A64-021.4 §20.
 *
 * ## The backend states facts; this chooses the sentence
 *
 * A notification arrives as a `type` plus one typed subject — an actor, a
 * tournament, a game result — and the functions below pick a **translation
 * key** and the values to interpolate into it. That is what makes the same
 * row readable in uz, ru and en.
 *
 * ## The one exception, and why it is not a hole — A64-031.D
 *
 * `platform_announcement` carries text an operator wrote, because no table
 * compiled into this build can hold a sentence a human writes on the day
 * they send it. It is the only type that does, the backend documents it as
 * such, and it changes nothing about markup: stored text is returned as
 * data here and rendered as a **text child** by the row, exactly as a
 * translated sentence is. No string from the server reaches the DOM as
 * markup on either path.
 *
 * ## Exactly one subject key is populated, and the type says which
 *
 * The response carries one nullable key per payload. A client could switch
 * on which one is present; it switches on `type` instead, and reads the key
 * that type promises. The difference matters when a backend adds another:
 * switching on presence would silently render the wrong branch, where
 * switching on type falls through to the generic sentence — which is what
 * §20's "unknown future type must render safely" asks for.
 *
 * ## Why `game_completed` is three keys and not one
 *
 * "You won", "you lost" and "you drew" are different sentences in every
 * language this product ships, and in uz and ru they are not one string with
 * a substituted word. The backend already resolved the outcome from the
 * recipient's point of view, so this only has to choose.
 */

/**
 * What a row says.
 *
 * Two forms, and a notification is exactly one of them — which is why this
 * is a union rather than an interface with optional fields. A row that
 * carried both a translation key and stored text would be a row with two
 * sentences and no rule for which wins.
 */
export type NotificationMessage =
  | {
      kind: "translated";
      key: TranslationKey;
      values: Record<string, string | number>;
    }
  | {
      /**
       * Text an operator wrote, rendered as-is — A64-031.D.
       *
       * The single exception to this module's rule, and it is the backend's
       * exception rather than this file's: `PLATFORM_ANNOUNCEMENT` is the
       * one type whose text is **stored** rather than composed from facts,
       * because no compiled table can hold a sentence a human writes on the
       * day they send it.
       *
       * It is still not markup. `title` and `body` are plain strings that
       * reach the DOM as text content, exactly as a translated sentence
       * does — React escapes them, and there is no `dangerouslySetInnerHTML`
       * anywhere on this path. The backend refuses control characters at
       * its own boundary, so what arrives is text in the ordinary sense.
       *
       * `locale` is the language it was written in, which is not
       * necessarily the reader's: the row marks the text with `lang` so a
       * screen reader pronounces Russian prose with Russian rules even when
       * the interface is in Uzbek.
       */
      kind: "stored";
      title: string;
      body: string;
      locale: string;
    };

export function notificationMessage(notification: Notification): NotificationMessage {
  switch (notification.type) {
    case "friend_request_received":
    case "friend_request_accepted":
      return {
        kind: "translated",
        key:
          notification.type === "friend_request_received"
            ? "notifications.types.friend_request_received"
            : "notifications.types.friend_request_accepted",
        values: { actor: actorNameOf(notification) },
      };

    case "tournament_registration_confirmed":
      return {
        kind: "translated",
        key: "notifications.types.tournament_registration_confirmed",
        values: { tournament: notification.tournament?.tournament_name ?? "" },
      };

    case "tournament_round_published":
      return {
        kind: "translated",
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
        kind: "translated",
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

    // A64-022.4 §19. Two types, one payload key, and the sentence names the
    // other player — the same shape the two friend types above use, because
    // "somebody challenged you" without a name is a notification you cannot
    // act on. The clock and whether it is rated are deliberately **not** in
    // the sentence: they are facts on the payload, and A64-022.5's challenge
    // surface is where they belong.
    case "friend_challenge_received":
    case "friend_challenge_accepted": {
      // An opponent whose account is gone arrives as `null` — the backend
      // keeps the challenge and loses the name — and a sentence beginning
      // with an empty string is one no language recovers from. Four keys is
      // the cost of never rendering one, exactly as `game_completed` pays it.
      const actor = challengeOpponentNameOf(notification);
      return {
        kind: "translated",
        key: challengeKey(notification.type, { named: actor !== "" }),
        values: { actor },
      };
    }

    // A64-031.D. The stored-text branch — see `NotificationMessage`. An
    // announcement whose payload did not arrive falls through to the
    // generic sentence rather than rendering two empty lines, which is the
    // same posture every other branch takes towards a missing subject.
    case "platform_announcement": {
      const announcement = notification.announcement;
      if (announcement === null || announcement === undefined) {
        return { kind: "translated", key: "notifications.types.unknown", values: {} };
      }
      return {
        kind: "stored",
        title: announcement.title,
        body: announcement.body,
        locale: announcement.locale,
      };
    }

    case "game_completed": {
      const opponent = opponentNameOf(notification);
      return {
        kind: "translated",
        key: gameOutcomeKey(notification.game?.outcome, { named: opponent !== "" }),
        values: { opponent },
      };
    }

    default:
      return { kind: "translated", key: "notifications.types.unknown", values: {} };
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
  if (notification.challenge) {
    return {
      label: challengeOpponentNameOf(notification),
      thumbnailUrl: notification.challenge.opponent?.thumbnail_url ?? null,
    };
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

function challengeKey(type: string, { named }: { named: boolean }): TranslationKey {
  if (type === "friend_challenge_received") {
    return named
      ? "notifications.types.friend_challenge_received"
      : "notifications.types.friend_challenge_received_anonymous";
  }
  return named
    ? "notifications.types.friend_challenge_accepted"
    : "notifications.types.friend_challenge_accepted_anonymous";
}

function challengeOpponentNameOf(notification: Notification): string {
  const opponent = notification.challenge?.opponent;
  return opponent === null || opponent === undefined
    ? ""
    : (opponent.display_name ?? opponent.username);
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
