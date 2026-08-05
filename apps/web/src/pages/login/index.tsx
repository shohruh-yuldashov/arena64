import { zodResolver } from "@hookform/resolvers/zod";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { messageKeyFor } from "@/features/auth/model/error-messages";
import { safeRedirect } from "@/features/auth/model/safe-redirect";
import { useSession } from "@/features/auth/model/session-provider";
import { loginSchema, type LoginValues } from "@/features/auth/schemas";
import { FormField } from "@/features/auth/ui/form-field";
import { FormError } from "@/features/auth/ui/form-status";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";
import { AuthShell } from "@/widgets/auth-shell";

/**
 * Sign in.
 *
 * ## What is deliberately not shown
 *
 * Anything that would distinguish "no such account" from "wrong password".
 * The backend returns one code for both on purpose, so this renders one
 * message for both — a client that split them would reintroduce the
 * account-enumeration oracle the API was written to avoid.
 *
 * Nothing is logged. Not the email, not the password, not on failure.
 *
 * ## The redirect
 *
 * `next` comes from the query string, which means it comes from whoever
 * built the link. `safeRedirect` decides where the user actually goes; see
 * that module on why an unvalidated `next` is a phishing primitive.
 */
export default function LoginPage() {
  const { t } = useTranslation();
  const { signIn } = useSession();
  const navigate = useNavigate();
  const { next } = useSearch({ from: "/login" });
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    mode: "onTouched",
    defaultValues: { email: "", password: "" },
  });

  const onSubmit = handleSubmit(async (values) => {
    setFailure(null);
    try {
      await signIn(values);
      await navigate({ to: safeRedirect(next), replace: true });
    } catch (error) {
      // The thrown value never reaches the DOM — only a key does.
      setFailure(messageKeyFor(error));
    }
  });

  return (
    <AuthShell
      title={t("auth.login.title")}
      description={t("auth.login.subtitle")}
      footer={
        <>
          {t("auth.login.noAccount")}{" "}
          <Link
            to="/register"
            search={next !== undefined ? { next } : {}}
            className="text-foreground underline underline-offset-4"
          >
            {t("auth.login.register")}
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
          label={t("auth.common.email")}
          type="email"
          autoComplete="email"
          autoFocus
          error={errors.email ? t(errors.email.message as TranslationKey) : undefined}
          {...register("email")}
        />

        <FormField
          label={t("auth.common.password")}
          type="password"
          autoComplete="current-password"
          error={errors.password ? t(errors.password.message as TranslationKey) : undefined}
          {...register("password")}
        />

        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting ? (
            <Spinner label={t("auth.common.submitting")} />
          ) : (
            t("auth.login.submit")
          )}
        </Button>

        <Link
          to="/forgot-password"
          className="text-muted-foreground hover:text-foreground self-start text-sm underline underline-offset-4"
        >
          {t("auth.login.forgot")}
        </Link>
      </form>
    </AuthShell>
  );
}
