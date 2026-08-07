import { zodResolver } from "@hookform/resolvers/zod";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { messageKeyFor } from "@/features/auth/model/error-messages";
import { useSession } from "@/features/auth/model/session-provider";
import { registerSchema, type RegisterValues } from "@/features/auth/schemas";
import { FormField, usePasswordHint } from "@/features/auth/ui/form-field";
import { FormError } from "@/features/auth/ui/form-status";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";
import { AuthShell } from "@/widgets/auth-shell";

/**
 * Create an account.
 *
 * ## The fields are the backend's, plus exactly one that is not
 *
 * `username`, `email` and `password` are `RegisterRequest`'s. `preferred_language`
 * and `timezone` are filled in from the browser rather than asked for
 * (see `SignUpPayload`), and `display_name` is left to its default.
 *
 * `passwordConfirmation` is **client-only** and is never sent: the backend
 * forbids extra fields, so including it would turn every sign-up into a
 * `422`. It exists because a typo in a field nobody can read is otherwise
 * discovered at the next sign-in, by which time the person does not know
 * what they typed.
 *
 * ## Why success now goes to `/verify-email` — A64-021.5H
 *
 * `POST /auth/browser/register` still signs the browser in, and the account
 * is still unverified. What changed is that "whatever the platform later
 * gates on that" now exists: every product write is refused for an
 * unverified account, so landing somebody in the app would put them on a
 * screen whose every button answers `403`.
 *
 * They go to the one page that has something for them to do, carrying the
 * `next` they arrived with — so a deep link survives the detour.
 */
export default function RegisterPage() {
  const { t, locale } = useTranslation();
  const { signUp } = useSession();
  const navigate = useNavigate();
  const { next } = useSearch({ from: "/register" });
  const passwordHint = usePasswordHint();
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<RegisterValues>({
    resolver: zodResolver(registerSchema),
    mode: "onTouched",
    defaultValues: { username: "", email: "", password: "", passwordConfirmation: "" },
  });

  const onSubmit = handleSubmit(async (values) => {
    setFailure(null);
    try {
      // `passwordConfirmation` is destructured away rather than deleted, so
      // there is no path by which it reaches the request body.
      await signUp({
        username: values.username,
        email: values.email,
        password: values.password,
        preferredLanguage: locale,
      });
      // **Not into the app** — A64-021.5H §18. The session exists and the
      // address does not yet, so every product write behind the app would
      // answer `403`. `next` travels along, so somebody who arrived from a
      // deep link still lands there once they have verified.
      await navigate({ to: "/verify-email", search: { next }, replace: true });
    } catch (error) {
      setFailure(messageKeyFor(error));
    }
  });

  return (
    <AuthShell
      title={t("auth.register.title")}
      description={t("auth.register.subtitle")}
      footer={
        <>
          {t("auth.register.haveAccount")}{" "}
          <Link
            to="/login"
            search={next !== undefined ? { next } : {}}
            className="text-foreground underline underline-offset-4"
          >
            {t("auth.register.login")}
          </Link>
        </>
      }
    >
      <form
        onSubmit={(event) => void onSubmit(event)}
        className="flex flex-col gap-4"
        noValidate
      >
        <FormError messageKey={failure} />

        <FormField
          label={t("auth.common.username")}
          autoComplete="username"
          autoFocus
          error={errors.username ? t(errors.username.message as TranslationKey) : undefined}
          {...register("username")}
        />

        <FormField
          label={t("auth.common.email")}
          type="email"
          autoComplete="email"
          error={errors.email ? t(errors.email.message as TranslationKey) : undefined}
          {...register("email")}
        />

        <FormField
          label={t("auth.common.password")}
          type="password"
          autoComplete="new-password"
          // The policy is shown before submission rather than discovered
          // one 422 at a time.
          description={passwordHint}
          error={errors.password ? t(errors.password.message as TranslationKey) : undefined}
          {...register("password")}
        />

        <FormField
          label={t("auth.common.confirmPassword")}
          type="password"
          autoComplete="new-password"
          error={
            errors.passwordConfirmation
              ? t(errors.passwordConfirmation.message as TranslationKey)
              : undefined
          }
          {...register("passwordConfirmation")}
        />

        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting ? (
            <Spinner label={t("auth.common.submitting")} />
          ) : (
            t("auth.register.submit")
          )}
        </Button>
      </form>
    </AuthShell>
  );
}
