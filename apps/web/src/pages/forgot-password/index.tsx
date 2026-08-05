import { zodResolver } from "@hookform/resolvers/zod";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { forgotPassword } from "@/features/auth/api";
import { messageKeyFor } from "@/features/auth/model/error-messages";
import { forgotPasswordSchema, type ForgotPasswordValues } from "@/features/auth/schemas";
import { FormField } from "@/features/auth/ui/form-field";
import { FormError, FormStatus } from "@/features/auth/ui/form-status";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";
import { AuthShell } from "@/widgets/auth-shell";

/**
 * Ask for a reset link.
 *
 * ## The success message is deliberately non-committal
 *
 * "If an account exists for that address, we have sent a link" — never
 * "we sent you an email", and never "no such account". The backend already
 * answers identically either way; a client that reported the difference
 * would turn this form into a way to test whether an address is registered,
 * which is exactly the reconnaissance the neutral response prevents.
 *
 * The same message is shown for **every** outcome that is not a client or
 * server fault, including a rate limit — see below.
 */
export default function ForgotPasswordPage() {
  const { t } = useTranslation();
  const [sent, setSent] = useState(false);
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<ForgotPasswordValues>({
    resolver: zodResolver(forgotPasswordSchema),
    mode: "onTouched",
    defaultValues: { email: "" },
  });

  const onSubmit = handleSubmit(async (values) => {
    setFailure(null);
    try {
      await forgotPassword(values);
      setSent(true);
    } catch (error) {
      setFailure(messageKeyFor(error));
    }
  });

  if (sent) {
    return (
      <AuthShell
        title={t("auth.forgotPassword.sentTitle")}
        footer={
          <Link to="/login" className="text-foreground underline underline-offset-4">
            {t("auth.forgotPassword.backToLogin")}
          </Link>
        }
      >
        <FormStatus>{t("auth.forgotPassword.sentBody")}</FormStatus>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title={t("auth.forgotPassword.title")}
      description={t("auth.forgotPassword.subtitle")}
      footer={
        <Link to="/login" className="text-foreground underline underline-offset-4">
          {t("auth.forgotPassword.backToLogin")}
        </Link>
      }
    >
      <form
        onSubmit={(event) => void onSubmit(event)}
        className="flex flex-col gap-4"
        noValidate
      >
        <FormError messageKey={failure} />

        <FormField
          label={t("auth.common.email")}
          type="email"
          autoComplete="email"
          autoFocus
          error={errors.email ? t(errors.email.message as TranslationKey) : undefined}
          {...register("email")}
        />

        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting ? (
            <Spinner label={t("auth.common.submitting")} />
          ) : (
            t("auth.forgotPassword.submit")
          )}
        </Button>
      </form>
    </AuthShell>
  );
}
