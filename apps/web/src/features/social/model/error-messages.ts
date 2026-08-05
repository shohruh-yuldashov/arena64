import { ApiError, type ErrorCode } from "@/shared/api";
import type { TranslationKey } from "@/shared/i18n";

/**
 * Backend error codes to translation keys — A64-020.4 §17.
 *
 * ## Only codes the registry actually has
 *
 * `app/core/error_codes.py` publishes two social-specific codes —
 * `duplicate_friend_request` and `opposite_friend_request_pending` — plus
 * the platform's generic ones. It has **no** `cannot_friend_self`,
 * `already_friends`, `already_blocked`, `not_blocked`, `friendship_not_found`
 * or `cooldown_active`, so none is mapped here: three languages of strings
 * for codes the server cannot send would be dead weight, and a table with
 * invented entries is one nobody can trust.
 *
 * Those situations still reach the user — as `conflict` or `not_found`,
 * which is what the API returns for them.
 *
 * ## Two codes that are really one message
 *
 * `duplicate_friend_request` and `opposite_friend_request_pending` both
 * mean "there is already a request between you two", and the user's next
 * step is the same: look at their requests. They are kept distinct anyway,
 * because *which* list to look in differs and that is worth saying.
 */
const CODE_MESSAGES: Partial<Record<ErrorCode, TranslationKey>> = {
  duplicate_friend_request: "social.errors.duplicate_friend_request",
  opposite_friend_request_pending: "social.errors.opposite_friend_request_pending",
  not_found: "social.errors.not_found",
  conflict: "social.errors.conflict",
  permission_denied: "social.errors.permission_denied",
  validation_error: "social.errors.validation_error",
  rate_limited: "social.errors.rate_limited",
};

/** The key to render for a thrown value. Never throws. */
export function socialErrorKey(error: unknown): TranslationKey {
  if (!(error instanceof ApiError)) return "social.errors.unexpected";
  if (error.kind === "network") return "social.errors.network";
  if (error.code !== null && error.code in CODE_MESSAGES) {
    return CODE_MESSAGES[error.code] ?? "social.errors.unexpected";
  }
  return "social.errors.unexpected";
}
