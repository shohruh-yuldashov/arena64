import { ApiError, type ErrorCode } from "@/shared/api";
import type { TranslationKey } from "@/shared/i18n";

/**
 * A backend error code to a translation key — A64-021.3 §20.
 *
 * Branch on the **code**, never on the message: the message is English
 * prose written for an operator, it changes when somebody rewords it, and
 * it cannot be translated.
 *
 * ## Three codes, three different sentences
 *
 * The backend answers `422` for all three, and collapsing them would be
 * the failure §20 exists to prevent — telling a player that push
 * notifications are "not allowed" when the truth is that they are not built
 * yet, or showing either message for what is actually a bug in this client.
 *
 * `duplicate_preference_change` is unreachable from this form: the matrix
 * renders one control per pair and the dirty set is keyed on the pair. It
 * is mapped anyway, because a code the server can send and this table
 * cannot name would render as "something went wrong" and lose the one
 * clue that would identify it.
 */
const CODE_MESSAGES: Partial<Record<ErrorCode, TranslationKey>> = {
  notification_preference_locked: "notificationPreferences.errors.locked",
  notification_channel_unavailable: "notificationPreferences.errors.unavailable",
  duplicate_preference_change: "notificationPreferences.errors.duplicate",
  validation_error: "notificationPreferences.errors.validation",
  rate_limited: "notificationPreferences.errors.rateLimited",
};

/** The key to render for a thrown value. Never throws. */
export function preferenceErrorKey(error: unknown): TranslationKey {
  if (!(error instanceof ApiError)) return "notificationPreferences.errors.unexpected";
  if (error.kind === "network") return "notificationPreferences.errors.network";
  if (error.code !== null && error.code in CODE_MESSAGES) {
    return CODE_MESSAGES[error.code] ?? "notificationPreferences.errors.unexpected";
  }
  return "notificationPreferences.errors.unexpected";
}
