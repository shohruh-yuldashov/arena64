import { z } from "zod";

import type { TranslationKey } from "@/shared/i18n";

/**
 * The form schemas, and the constants they enforce.
 *
 * ## Why the numbers are duplicated from the backend, and how that is kept honest
 *
 * These mirror `app/modules/auth/domain/validators.py` and
 * `app/modules/users/domain/validators.py`. They are **not** generated:
 * OpenAPI describes types and lengths, not "must contain a digit", so the
 * password policy has no generated form to import.
 *
 * The duplication is deliberate and bounded. Client validation exists so a
 * person is told their password is too short **while typing** rather than
 * after a round trip — it is a courtesy, never the guarantee. The backend
 * re-validates everything and its answer is the one that counts, so the
 * worst case if these drift is a 422 the form displays, not an invalid
 * account. Anything stricter here than there would be worse: a user
 * blocked from a password the platform would have accepted.
 *
 * ## Messages are keys, resolved at render
 *
 * Zod is configured at module scope, before a locale is chosen, so a schema
 * cannot hold translated text. It carries **translation keys** and the form
 * resolves them — which also means switching language re-renders the errors
 * in the new one rather than leaving the previous language's text behind.
 */
export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 128;
export const USERNAME_MIN_LENGTH = 3;
export const USERNAME_MAX_LENGTH = 20;

/** `^[a-zA-Z0-9][a-zA-Z0-9_]*$` — `users.domain.validators._USERNAME_PATTERN`. */
const USERNAME_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_]*$/;

/** `auth.domain.validators.SPECIAL_CHARACTERS`, as a character class. */
const SPECIAL_CHARACTER = /[!-/:-@[-`{-~]/;

const key = (translation: TranslationKey): string => translation;

const email = z
  .string()
  .min(1, key("auth.validation.emailRequired"))
  // Trimmed and lower-cased to match what the backend's `EmailField` does,
  // so " Alice@Example.COM " and "alice@example.com" are one account here
  // as well — which they must be, or a phone keyboard that capitalises the
  // first letter locks people out of their own account.
  .transform((value) => value.trim().toLowerCase())
  .pipe(z.email(key("auth.validation.emailInvalid")));

const password = z
  .string()
  .min(1, key("auth.validation.passwordRequired"))
  .min(PASSWORD_MIN_LENGTH, key("auth.validation.passwordTooShort"))
  .max(PASSWORD_MAX_LENGTH, key("auth.validation.passwordTooLong"))
  .refine(
    (value) =>
      /[A-Z]/.test(value) &&
      /[a-z]/.test(value) &&
      /\d/.test(value) &&
      SPECIAL_CHARACTER.test(value),
    key("auth.validation.passwordWeak"),
  );

const username = z
  .string()
  .min(1, key("auth.validation.usernameRequired"))
  // Trimmed only. **Never lower-cased**: the backend stores the
  // capitalisation the player chose and folds only for comparison, so
  // folding here would silently register a different name from the one
  // they typed.
  .transform((value) => value.trim())
  .pipe(
    z
      .string()
      .min(USERNAME_MIN_LENGTH, key("auth.validation.usernameTooShort"))
      .max(USERNAME_MAX_LENGTH, key("auth.validation.usernameTooLong"))
      .regex(USERNAME_PATTERN, key("auth.validation.usernameInvalid")),
  );

export const loginSchema = z.object({
  email,
  // The sign-in field is deliberately **not** policy-checked: the policy
  // may have changed since the account was created, and refusing to submit
  // an existing password because it lacks a symbol would lock somebody out
  // of their own account from the client side.
  password: z.string().min(1, key("auth.validation.passwordRequired")),
});
export type LoginValues = z.infer<typeof loginSchema>;

export const registerSchema = z
  .object({
    username,
    email,
    password,
    // **Client-only.** The backend has no such field (`RegisterRequest`
    // forbids extras), so it is stripped before the request is built — see
    // the register page. It exists because a typo in a password nobody can
    // see is otherwise discovered at the next sign-in.
    passwordConfirmation: z.string().min(1, key("auth.validation.passwordRequired")),
  })
  .refine((values) => values.password === values.passwordConfirmation, {
    message: key("auth.validation.passwordsDoNotMatch"),
    path: ["passwordConfirmation"],
  });
export type RegisterValues = z.infer<typeof registerSchema>;

export const forgotPasswordSchema = z.object({ email });
export type ForgotPasswordValues = z.infer<typeof forgotPasswordSchema>;

export const resendVerificationSchema = z.object({ email });
export type ResendVerificationValues = z.infer<typeof resendVerificationSchema>;

export const resetPasswordSchema = z
  .object({
    password,
    passwordConfirmation: z.string().min(1, key("auth.validation.passwordRequired")),
  })
  .refine((values) => values.password === values.passwordConfirmation, {
    message: key("auth.validation.passwordsDoNotMatch"),
    path: ["passwordConfirmation"],
  });
export type ResetPasswordValues = z.infer<typeof resetPasswordSchema>;
