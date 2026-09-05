import { type TranslationKey, useTranslation } from "@/shared/i18n";

/**
 * The platform's bounded vocabularies, as one map — A64-027A.3 §40.
 *
 * Every value below is a member of a backend `StrEnum`, and the console's
 * job is to render the word rather than the identifier. Before this task
 * `queue`, `russian_8x8`, `swiss`, `cheating` and `account` all reached the
 * screen verbatim: an administrator who did not build Arena64 had to decode
 * a database value from a table cell.
 *
 * ## Why a key builder rather than a lookup table
 *
 * A `Record<string, TranslationKey>` per enum would be twelve maps to keep
 * in step with twelve backend enums, and the failure mode is a *blank cell*
 * when the backend adds a member — which reads as "nothing happened".
 * Building the key means an unknown member renders as its own identifier,
 * which is the same fallback `AUDIT_ACTION_LABELS` chose for the same
 * reason: the platform outlives the console reading it.
 *
 * ## One source of truth
 *
 * `analytics` held a private copy of the termination labels. It now reads
 * these, because two answers to "what does `agreed_draw` say" is one answer
 * too many and the newer surface is silently the more complete one.
 */

/** The namespaces `vocab.*` defines. Adding one here is adding a key. */
export type VocabNamespace =
  | "matchOrigin"
  | "matchMode"
  | "variant"
  | "speedClass"
  | "outcome"
  | "side"
  | "termination"
  | "tournamentFormat"
  | "entrantStatus"
  | "notificationType"
  | "notificationCategory"
  | "sanctionKind"
  | "sanctionCategory"
  | "auditSubject"
  | "auditOutcome";

/**
 * The translation key for one enum member.
 *
 * The cast is the one this codebase already uses for dynamic keys: the
 * template cannot be proved to be a `TranslationKey` at compile time, and
 * `lookup` falls back to the key itself, so an unknown member degrades to
 * its identifier rather than to nothing.
 */
export function vocabKey(namespace: VocabNamespace, value: string): TranslationKey {
  return `vocab.${namespace}.${value}` as TranslationKey;
}

/**
 * The label for one enum member, with the **identifier** as its fallback.
 *
 * `t` returns the key it was given when nothing resolves, so calling it
 * directly puts `vocab.auditSubject.queue_ticket` on screen for a member
 * this build has not heard of — a raw translation key, which is worse than
 * the raw enum it replaced.
 *
 * So an unresolved lookup degrades to the value itself. An operator seeing
 * `queue_ticket` knows something happened and can search for it; an
 * operator seeing a translation key learns only that the console is broken.
 */
export function useVocab(): (namespace: VocabNamespace, value: string) => string {
  const { t } = useTranslation();
  return (namespace, value) => {
    const key = vocabKey(namespace, value);
    const label = t(key);
    return label === key ? value : label;
  };
}
