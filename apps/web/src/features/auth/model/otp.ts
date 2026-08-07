import { ApiError } from "@/shared/api";
import type { TranslationKey } from "@/shared/i18n";

/**
 * The six-digit verification code, on the client — A64-021.5H §20, §24.
 *
 * Length and error mapping only. The **policy** — how long a code lives,
 * how many guesses it survives, when another may be asked for — is the
 * server's, and every one of those answers arrives as a stable error code
 * rather than being recomputed here.
 */

/** Mirrors `auth.domain.otp.OTP_LENGTH`. */
export const OTP_LENGTH = 6;

/**
 * A backend code to a translation key.
 *
 * Branch on the **code**, never on the message: the message is English
 * prose for an operator and cannot be translated.
 *
 * Four codes, four different sentences and four different next actions —
 * which is exactly why the backend spends four codes on one screen. A
 * client that collapsed them would tell somebody to retype a code that has
 * expired.
 */
const CODE_MESSAGES: Partial<Record<string, TranslationKey>> = {
  email_verification_code_invalid: "auth.verifyEmail.errors.invalid",
  email_verification_code_expired: "auth.verifyEmail.errors.expired",
  email_verification_attempts_exceeded: "auth.verifyEmail.errors.exhausted",
  email_verification_resend_too_soon: "auth.verifyEmail.errors.tooSoon",
  rate_limited: "auth.verifyEmail.errors.tooSoon",
};

/** The key to render for a thrown value. Never throws. */
export function otpErrorKey(error: unknown): TranslationKey {
  if (!(error instanceof ApiError) || error.code === null) {
    return "auth.verifyEmail.errors.unexpected";
  }
  return CODE_MESSAGES[error.code] ?? "auth.verifyEmail.errors.unexpected";
}

/**
 * How long until another code may be asked for, in whole seconds.
 *
 * Read from the server's `Retry-After`, never invented. §11 and §22 are
 * explicit that a frontend countdown is presentation: the authority is a
 * durable row, so a reload, a second tab and a second node agree, and a
 * client that guessed would offer a button the server refuses.
 *
 * `0` when the server said nothing, which means "ask and find out" rather
 * than "wait forever".
 */
export function cooldownFrom(error: unknown): number {
  if (!(error instanceof ApiError)) return 0;
  return Math.max(0, error.retryAfterSeconds ?? 0);
}

/** Enough of an address to recognise, and not enough to publish — §21. */
export function maskEmail(email: string): string {
  const [local, domain] = email.split("@");
  if (domain === undefined || local === undefined || local.length === 0) return email;
  const head = local.slice(0, 1);
  return `${head}${"•".repeat(Math.max(local.length - 1, 1))}@${domain}`;
}
