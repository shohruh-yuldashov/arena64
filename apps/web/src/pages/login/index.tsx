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
import { GoogleOutcomeNotice, GoogleSignIn } from "@/features/auth/ui/google-sign-in";
import { PasswordField } from "@/features/auth/ui/password-field";
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
 *
 * ## Google — A64-031.E1
 *
 * "Continue with Google" sits above the form when the deployment offers
 * it, and `?google=` is what a Google round trip came back to say when it
 * signed nobody in. Both are additions; the form is unchanged.
 */
export default function LoginPage() {
  const { t } = useTranslation();
  const { signIn } = useSession();
  const navigate = useNavigate();
  const { next, google } = useSearch({ from: "/login" });
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    mode: "onTouched",
    defaultValues: { identifier: "", password: "" },
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
        <GoogleOutcomeNotice outcome={google} />
        <GoogleSignIn next={next} />

        <FormError messageKey={failure} />

        {/* `type="text"`, not `email`: a username has no `@`, and the
            browser's own email check would refuse it before submit.
            `autoComplete="username"` is the token password managers pair
            with `current-password`, whichever kind is stored. */}
        <FormField
          label={t("auth.common.identifier")}
          type="text"
          autoComplete="username"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          autoFocus
          error={errors.identifier ? t(errors.identifier.message as TranslationKey) : undefined}
          {...register("identifier")}
        />

        <PasswordField
          label={t("auth.common.password")}
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

        {/* A standalone action rather than a link inside a sentence, so it
            gets the same 44px floor every other control has (§7). */}
        <Link
          to="/forgot-password"
          className="text-muted-foreground hover:text-foreground focus-visible:ring-ring -ml-1 inline-flex min-h-11 items-center self-start rounded-md px-1 text-sm underline underline-offset-4 focus-visible:ring-2 focus-visible:outline-none"
        >
          {t("auth.login.forgot")}
        </Link>
      </form>
    </AuthShell>
  );
}
