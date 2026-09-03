import type { TranslationKey } from "@/shared/i18n";

/**
 * The backend's vocabulary, mapped to translation keys — A64-020.6 §14, §23.
 *
 * Every table below is keyed by a **real server enum value**, cross-checked
 * against the module that owns it:
 *
 *     TournamentStatus   app/modules/tournament/domain/tournament.py
 *     TournamentFormat   the same file
 *     RoundStatus        app/modules/tournament/domain/rounds.py
 *     FinalStatus        app/modules/tournament/domain/standings.py
 *
 * The lookups are total in one direction only, deliberately: a value this
 * build has never seen falls through to an explicit "unknown" rather than
 * rendering the raw identifier. A64-020.5B's lesson is why — three invented
 * termination-reason names meant an agreed draw displayed as "Unknown" for
 * two phases, because nothing failed loudly and nothing looked wrong.
 *
 * `Record<string, …>` rather than `Record<TournamentStatus, …>` on purpose:
 * these values arrive over the wire, and typing the *input* as a closed
 * union would let a backend addition compile here and be rendered as a
 * blank cell in production.
 */

const STATUS: Record<string, TranslationKey> = {
  draft: "tournament.status.draft",
  registration_open: "tournament.status.registration_open",
  registration_closed: "tournament.status.registration_closed",
  in_progress: "tournament.status.in_progress",
  completed: "tournament.status.completed",
  cancelled: "tournament.status.cancelled",
};

export function statusKey(status: string): TranslationKey {
  return STATUS[status] ?? "tournament.status.unknown";
}

const FORMAT: Record<string, TranslationKey> = {
  single_elimination: "tournament.format.single_elimination",
  double_elimination: "tournament.format.double_elimination",
  swiss: "tournament.format.swiss",
  round_robin: "tournament.format.round_robin",
  arena: "tournament.format.arena",
};

export function formatKey(format: string): TranslationKey {
  return FORMAT[format] ?? "tournament.format.unknown";
}

/**
 * The variant and the speed class — A64-025.7.
 *
 * Both were rendering the **raw server enum**: a tournament card said
 * `russian_8x8` and `classical` to a player, in every locale, on the lobby
 * and on the detail page. Nothing failed; the identifiers simply arrived on
 * screen, which is the same failure the note above describes.
 *
 * The keys live under `play.*` because that is where this vocabulary already
 * was — `play.speed` has carried all five classes since the lobby was built,
 * and a second copy under `tournament.*` would be two places to add
 * `correspondence` when it ships. `play.variant` is new only because no
 * surface had ever needed to name a variant: the game room does not, since
 * a player who is playing knows what they are playing.
 */
const VARIANT: Record<string, TranslationKey> = {
  russian_8x8: "play.variant.russian_8x8",
};

export function variantKey(variant: string): TranslationKey {
  return VARIANT[variant] ?? "play.variant.unknown";
}

const SPEED_CLASS: Record<string, TranslationKey> = {
  bullet: "play.speed.bullet",
  blitz: "play.speed.blitz",
  rapid: "play.speed.rapid",
  classical: "play.speed.classical",
  correspondence: "play.speed.correspondence",
};

export function speedClassKey(speedClass: string): TranslationKey {
  return SPEED_CLASS[speedClass] ?? "play.speed.unknown";
}

const ROUND_STATUS: Record<string, TranslationKey> = {
  pending: "tournament.bracket.roundStatus.pending",
  published: "tournament.bracket.roundStatus.published",
  in_progress: "tournament.bracket.roundStatus.in_progress",
  completed: "tournament.bracket.roundStatus.completed",
};

export function roundStatusKey(status: string): TranslationKey {
  return ROUND_STATUS[status] ?? "tournament.bracket.roundStatus.unknown";
}

const FINAL_STATUS: Record<string, TranslationKey> = {
  champion: "tournament.finalStatus.champion",
  runner_up: "tournament.finalStatus.runner_up",
  eliminated: "tournament.finalStatus.eliminated",
  withdrawn: "tournament.finalStatus.withdrawn",
};

export function finalStatusKey(status: string): TranslationKey {
  return FINAL_STATUS[status] ?? "tournament.finalStatus.unknown";
}

/**
 * A round's name, when the bracket's shape gives it one.
 *
 * Only the last three rounds are named, and only when the total is known:
 * "Quarter-finals" is a statement about *distance from the end*, so it
 * cannot be derived from a round number alone — round 2 of a four-player
 * bracket is the final, and round 2 of a 128-player one is not.
 *
 * Every other round keeps its number, which is what the backend publishes
 * and what a player reads on the bracket itself.
 */
export function roundNameKey(roundNumber: number, totalRounds: number): TranslationKey | null {
  const fromEnd = totalRounds - roundNumber;
  if (fromEnd === 0) return "tournament.bracket.final";
  if (fromEnd === 1) return "tournament.bracket.semifinal";
  if (fromEnd === 2) return "tournament.bracket.quarterfinal";
  return null;
}
