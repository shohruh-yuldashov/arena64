import { ApiError, type ErrorCode } from "@/shared/api";
import type { TranslationKey } from "@/shared/i18n";

/**
 * A backend error code to a translation key.
 *
 * ## Why a table and not a string match
 *
 * The backend's message is English prose written for an operator. Branching
 * on it — `message.includes("expired")` — breaks the moment somebody
 * rewords it, and it cannot be translated at all. The **code** is the
 * contract (`app/core/error_codes.py`), so the code is what is mapped.
 *
 * ## Why the table is bounded
 *
 * Only codes an auth screen can actually produce are listed. A map covering
 * all forty would be forty strings in three languages that no path reaches,
 * and the ones that mattered would be indistinguishable from the ones that
 * did not. Anything unlisted falls through to `unexpected`, which is the
 * honest message for a failure this feature did not anticipate.
 *
 * ## What is deliberately not distinguished
 *
 * `invalid_credentials` is one message for "no such account" and "wrong
 * password", because the backend returns one code for both — deliberately,
 * so the endpoint cannot be used to discover which addresses are
 * registered. Splitting them here would undo that from the client side.
 */
const CODE_MESSAGES: Partial<Record<ErrorCode, TranslationKey>> = {
  invalid_credentials: "auth.errors.invalid_credentials",
  inactive_account: "auth.errors.inactive_account",
  account_locked: "auth.errors.account_locked",
  authentication_required: "auth.errors.authentication_required",
  invalid_token: "auth.errors.invalid_token",
  expired_token: "auth.errors.expired_token",
  invalid_session: "auth.errors.invalid_session",
  session_expired: "auth.errors.session_expired",
  username_already_exists: "auth.errors.username_already_exists",
  email_already_exists: "auth.errors.email_already_exists",
  invalid_username: "auth.errors.invalid_username",
  invalid_email: "auth.errors.invalid_email",
  weak_password: "auth.errors.weak_password",
  invalid_verification_token: "auth.errors.invalid_verification_token",
  invalid_reset_token: "auth.errors.invalid_reset_token",
  validation_error: "auth.errors.validation_error",
  rate_limited: "auth.errors.rate_limited",
  permission_denied: "auth.errors.permission_denied",
};

/** The translation key to show for a thrown value. Never throws. */
export function messageKeyFor(error: unknown): TranslationKey {
  if (!(error instanceof ApiError)) {
    return "auth.errors.unexpected";
  }
  // A network failure has no code and its own message, because the user's
  // action is different: check the connection, not the credentials.
  if (error.kind === "network") {
    return "auth.errors.network";
  }
  if (error.code !== null && error.code in CODE_MESSAGES) {
    return CODE_MESSAGES[error.code] ?? "auth.errors.unexpected";
  }
  return "auth.errors.unexpected";
}

/**
 * Whether this failure means the session is gone rather than the request
 * was wrong — the distinction the interceptor and the session provider both
 * branch on.
 */
export function isSessionEnded(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (error.kind !== "http") return false;
  return (
    error.status === 401 || error.code === "invalid_session" || error.code === "session_expired"
  );
}
