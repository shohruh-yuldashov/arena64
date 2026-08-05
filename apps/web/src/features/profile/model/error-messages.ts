import { ApiError, type ErrorCode } from "@/shared/api";
import type { TranslationKey } from "@/shared/i18n";

/**
 * A backend error code to a translation key — the profile surfaces' table.
 *
 * Same policy as `features/auth`: branch on the **code**, never on the
 * message. The message is English prose for an operator; it changes when
 * somebody rewords it and cannot be translated at all.
 *
 * ## The codes here are the ones the registry actually has
 *
 * `app/core/error_codes.py` has no `profile_not_found`, `username_conflict`,
 * `invalid_avatar` or `unsupported_media_type`. What it has is `not_found`,
 * `conflict`, `username_already_exists`, `invalid_username` and
 * `avatar_too_large` — so those are what is mapped. Inventing entries for
 * codes the server cannot send would be three languages of dead strings
 * and a table nobody could trust.
 *
 * A rejected media type arrives as `validation_error` from the upload
 * endpoint, which the generic message covers; the client's own check
 * catches the common case before the request is made.
 */
const CODE_MESSAGES: Partial<Record<ErrorCode, TranslationKey>> = {
  not_found: "profile.errors.not_found",
  permission_denied: "profile.errors.permission_denied",
  conflict: "profile.errors.conflict",
  username_already_exists: "profile.errors.username_already_exists",
  invalid_username: "profile.errors.invalid_username",
  avatar_too_large: "profile.errors.avatar_too_large",
  validation_error: "profile.errors.validation_error",
  rate_limited: "profile.errors.rate_limited",
};

/** The key to render for a thrown value. Never throws. */
export function profileErrorKey(error: unknown): TranslationKey {
  if (!(error instanceof ApiError)) return "profile.errors.unexpected";
  if (error.kind === "network") return "profile.errors.network";
  if (error.code !== null && error.code in CODE_MESSAGES) {
    return CODE_MESSAGES[error.code] ?? "profile.errors.unexpected";
  }
  return "profile.errors.unexpected";
}

/** Whether a failed read means "no such player" rather than "try again". */
export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.kind === "http" && error.status === 404;
}
