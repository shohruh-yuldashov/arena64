import { zodResolver } from "@hookform/resolvers/zod";
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";

import { messageKeyFor } from "@/features/auth/model/error-messages";
import { useGooglePending } from "@/features/auth/model/google";
import { safeRedirect } from "@/features/auth/model/safe-redirect";
import { useSession } from "@/features/auth/model/session-provider";
import { connectGoogleSchema, type ConnectGoogleValues } from "@/features/auth/schemas";
import { FormError } from "@/features/auth/ui/form-status";
import { GoogleSignInExpired } from "@/features/auth/ui/google-sign-in";
import { PasswordField } from "@/features/auth/ui/password-field";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";
import { AuthShell } from "@/widgets/auth-shell";

/**
 * Connect Google to the account that already uses its address —
 * A64-031.E1, ADR-008.
 *
 * The explicit step. Google has proven who the person is at Google; only
 * this account's password proves they are the person who owns *this*
 * account, and an address match alone never links the two. Entered once:
 * afterwards "Continue with Google" signs straight in.
 *
 * No identifier field — the account was fixed when Google returned and is
 * not the page's to choose. Somebody who does not know the password has
 * the same way in as anyone: "Forgot your password?", through the address
 * Google has just vouched for.
 */
export default function ConnectGooglePage() {
  const { t } = useTranslation();
  const { connectGoogle } = useSession();
  const navigate = useNavigate();
  const pending = useGooglePending();
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<ConnectGoogleValues>({
    resolver: zodResolver(connectGoogleSchema),
    mode: "onTouched",
    defaultValues: { password: "" },
  });

  const purpose = pending.data?.purpose;
  useEffect(() => {
    if (purpose === "create_account") void navigate({ to: "/register/google", replace: true });
  }, [navigate, purpose]);

  const onSubmit = handleSubmit(async (values) => {
    setFailure(null);
    try {
      await connectGoogle(values.password);
      await navigate({ to: safeRedirect(pending.data?.next), replace: true });
    } catch (error) {
      setFailure(messageKeyFor(error));
    }
  });

  if (pending.isError) {
    return (
      <AuthShell title={t("auth.google.expiredTitle")}>
        <GoogleSignInExpired error={pending.error} />
      </AuthShell>
    );
  }
  if (pending.data?.purpose !== "link_account") {
    return (
      <AuthShell title={t("auth.google.link.title")}>
        <div className="flex justify-center py-6">
          <Spinner label={t("auth.google.pending")} />
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title={t("auth.google.link.title")}
      description={t("auth.google.link.subtitle", { email: pending.data.email })}
      footer={
        <Link to="/login" className="text-foreground underline underline-offset-4">
          {t("auth.google.link.cancel")}
        </Link>
      }
    >
      <form
        onSubmit={(event) => void onSubmit(event)}
        className="flex flex-col gap-4"
        noValidate
      >
        <FormError messageKey={failure} />

        <PasswordField
          label={t("auth.common.password")}
          autoComplete="current-password"
          autoFocus
          error={errors.password ? t(errors.password.message as TranslationKey) : undefined}
          {...register("password")}
        />

        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting ? (
            <Spinner label={t("auth.common.submitting")} />
          ) : (
            t("auth.google.link.submit")
          )}
        </Button>

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
