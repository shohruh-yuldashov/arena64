import { ApiError, type ErrorCode } from "@/shared/api";
import type { TranslationKey } from "@/shared/i18n";

/**
 * Backend error codes to translation keys — A64-022.5 §5.
 *
 * ## Only codes the registry actually has
 *
 * `app/core/error_codes.py` publishes six challenge-specific codes and this
 * maps all six, plus the generic ones a client can act on. Nothing is
 * invented: A64-022.2 records deliberately that there is **no**
 * `challenge_blocked` — a challenge to a blocked player answers exactly as
 * one to a non-friend does, so that a block stays invisible — and no
 * `challenge_forbidden`, because naming which party the caller was would
 * disclose the other side of a challenge they may not see.
 *
 * Mapping either would be three languages of strings for a code the server
 * cannot send, and would invite somebody to reason about a distinction the
 * API refuses to make.
 *
 * ## Validation lives on the server
 *
 * §5: the dialog submits and renders what comes back. There is no
 * client-side friendship check, no client-side duplicate check and no
 * client-side expiry check — each would be a second copy of a rule the
 * backend re-evaluates inside the transaction anyway, and the copy is the
 * one that would be wrong.
 */
const CODE_MESSAGES: Partial<Record<ErrorCode, TranslationKey>> = {
  challenge_self_not_allowed: "challenges.errors.self",
  // Also what a **blocked** pair gets, deliberately — see above.
  challenge_not_friends: "challenges.errors.notFriends",
  challenge_already_pending: "challenges.errors.alreadyPending",
  challenge_not_pending: "challenges.errors.notPending",
  challenge_expired: "challenges.errors.expired",
  challenge_invalid_time_control: "challenges.errors.invalidTimeControl",
  not_found: "challenges.errors.notFound",
  conflict: "challenges.errors.conflict",
  permission_denied: "challenges.errors.permissionDenied",
  validation_error: "challenges.errors.validation",
  rate_limited: "challenges.errors.rateLimited",
};

/** The key to render for a thrown value. Never throws. */
export function challengeErrorKey(error: unknown): TranslationKey {
  if (!(error instanceof ApiError)) return "challenges.errors.unexpected";
  if (error.kind === "network") return "challenges.errors.network";
  if (error.code !== null && error.code in CODE_MESSAGES) {
    return CODE_MESSAGES[error.code] ?? "challenges.errors.unexpected";
  }
  return "challenges.errors.unexpected";
}
