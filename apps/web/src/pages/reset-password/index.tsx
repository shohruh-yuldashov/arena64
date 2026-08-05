import { zodResolver } from "@hookform/resolvers/zod";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { resetPassword } from "@/features/auth/api";
import { messageKeyFor } from "@/features/auth/model/error-messages";
import { resetPasswordSchema, type ResetPasswordValues } from "@/features/auth/schemas";
import { FormField, usePasswordHint } from "@/features/auth/ui/form-field";
import { FormError, FormStatus } from "@/features/auth/ui/form-status";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";
import { AuthShell } from "@/widgets/auth-shell";

/**
 * Set a new password from a mailed link.
 *
 * ## No automatic sign-in
 *
 * `POST /auth/password/reset` issues no session, so this does not pretend
 * one exists — it sends the user to sign in with the password they just
 * chose. Auto-signing-in here would mean inventing a session from a
 * credential the backend deliberately did not turn into one, and it would
 * sign in whoever holds the link rather than whoever owns the account.
 *
 * ## The token is read from the query string and never rendered
 *
 * It arrives in the URL because that is what a mail client can carry. It is
 * not displayed, not logged, and not put in a hidden input — a hidden input
 * would place it in the DOM, where any script can read it.
 */
export default function ResetPasswordPage() {
  const { t } = useTranslation();
  const { token } = useSearch({ from: "/reset-password" });
  const navigate = useNavigate();
  const passwordHint = usePasswordHint();
  const [done, setDone] = useState(false);
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<ResetPasswordValues>({
    resolver: zodResolver(resetPasswordSchema),
    mode: "onTouched",
    defaultValues: { password: "", passwordConfirmation: "" },
  });

  const onSubmit = handleSubmit(async (values) => {
    setFailure(null);
    try {
      await resetPassword({ token: token ?? "", password: values.password });
      setDone(true);
    } catch (error) {
      setFailure(messageKeyFor(error));
    }
  });

  if (token === undefined || token === "") {
    return (
      <AuthShell title={t("auth.resetPassword.title")}>
        <FormStatus>{t("auth.resetPassword.missingToken")}</FormStatus>
        <Button asChild variant="outline">
          <Link to="/forgot-password">{t("auth.forgotPassword.submit")}</Link>
        </Button>
      </AuthShell>
    );
  }

  if (done) {
    return (
      <AuthShell title={t("auth.resetPassword.successTitle")}>
        <FormStatus>{t("auth.resetPassword.successBody")}</FormStatus>
        <Button onClick={() => void navigate({ to: "/login", replace: true })}>
          {t("auth.login.submit")}
        </Button>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title={t("auth.resetPassword.title")}
      description={t("auth.resetPassword.subtitle")}
    >
      <form
        onSubmit={(event) => void onSubmit(event)}
        className="flex flex-col gap-4"
        noValidate
      >
        <FormError messageKey={failure} />

        <FormField
          label={t("auth.common.password")}
          type="password"
          autoComplete="new-password"
          autoFocus
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
            t("auth.resetPassword.submit")
          )}
        </Button>
      </form>
    </AuthShell>
  );
}
