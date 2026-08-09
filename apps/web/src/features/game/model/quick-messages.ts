import type { TranslationKey } from "@/shared/i18n";
import { QUICK_MESSAGES, type QuickMessage } from "@/shared/realtime";

/**
 * How each catalogue entry is presented — A64-023.2 §3, §4.
 *
 * The **one** closed mapping this feature has: semantic identifier ->
 * localisation key -> glyph. Nothing else in the client decides how a quick
 * message looks, and nothing anywhere builds a second list of what the six
 * are — `QUICK_MESSAGES` is the protocol's, re-exported from
 * `shared/realtime`, and this only adds presentation to it.
 *
 * ## Why a `Record` keyed by the union, and not an array of objects
 *
 * An array would let an entry be missing or duplicated and still compile.
 * `Record<QuickMessage, …>` makes the exhaustiveness the *type*: adding
 * `sorry` to the gateway's catalogue and to `QuickMessage` fails the build
 * here until it has a label and a glyph. That is the whole reason this
 * mapping exists rather than being inlined into the picker.
 *
 * ## The glyphs are presentation only — §4
 *
 * The protocol value is `nice_move`; the glyph is decoration this client
 * chose and may change without touching the server. They are deliberately
 * **restrained and never hostile**: a laughing or shrugging face after an
 * opponent's blunder is a taunt with deniability, and ADR-004's whole
 * argument is that no entry should be capable of one. Every glyph below is
 * a handshake, applause, a thumbs-up or a mild self-directed wince.
 *
 * They are also `aria-hidden` wherever rendered — the localised text is the
 * accessible content, and an emoji read aloud as "person with folded hands"
 * in the middle of a game is noise.
 */
export interface QuickMessagePresentation {
  label: TranslationKey;
  /** Decoration. Never the message, never announced. */
  glyph: string;
}

export const QUICK_MESSAGE_PRESENTATION: Record<QuickMessage, QuickMessagePresentation> = {
  good_luck: { label: "game.quickMessages.goodLuck", glyph: "🤝" },
  nice_move: { label: "game.quickMessages.niceMove", glyph: "👏" },
  well_played: { label: "game.quickMessages.wellPlayed", glyph: "👍" },
  good_game: { label: "game.quickMessages.goodGame", glyph: "🤝" },
  thanks: { label: "game.quickMessages.thanks", glyph: "🙂" },
  oops: { label: "game.quickMessages.oops", glyph: "😅" },
};

/**
 * The catalogue in the order a picker offers it.
 *
 * Re-exported rather than redeclared, so there is exactly one list. A second
 * array here is the "uncontrolled duplicate catalogue" §3 forbids, and it
 * would drift the first time an entry is removed.
 */
export const QUICK_MESSAGE_ORDER = QUICK_MESSAGES;
