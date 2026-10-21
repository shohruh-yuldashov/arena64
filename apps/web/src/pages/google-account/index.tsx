import { zodResolver } from "@hookform/resolvers/zod";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";

import { messageKeyFor } from "@/features/auth/model/error-messages";
import { useGooglePending } from "@/features/auth/model/google";
import { safeRedirect } from "@/features/auth/model/safe-redirect";
import { useSession } from "@/features/auth/model/session-provider";
import { googleAccountSchema, type GoogleAccountValues } from "@/features/auth/schemas";
import { FormField } from "@/features/auth/ui/form-field";
import { FormError } from "@/features/auth/ui/form-status";
import { GoogleSignInExpired } from "@/features/auth/ui/google-sign-in";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";
import { AuthShell } from "@/widgets/auth-shell";

/**
 * Choose a username — the one thing a Google sign-in cannot supply.
 * A64-031.E1.
 *
 * Reached only from the API's Google callback, for a verified address no
 * account uses. The address is Google's and is shown, not asked for; there
 * is no password, because the account signs in with Google. Created
 * already verified, so success goes straight to where the person was
 * going — not through `/verify-email` as a password registration does.
 *
 * A name is never invented on the person's behalf: a handle chosen for
 * somebody is one they then have to discover, and cannot change.
 */
export default function GoogleAccountPage() {
  const { t, locale } = useTranslation();
  const { createGoogleAccount } = useSession();
  const navigate = useNavigate();
  const pending = useGooglePending();
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<GoogleAccountValues>({
    resolver: zodResolver(googleAccountSchema),
    mode: "onTouched",
    defaultValues: { username: "" },
  });

  const purpose = pending.data?.purpose;
  useEffect(() => {
    // The other step's identity landed here: send it where it belongs.
    if (purpose === "link_account") void navigate({ to: "/connect-google", replace: true });
  }, [navigate, purpose]);

  const onSubmit = handleSubmit(async (values) => {
    setFailure(null);
    try {
      await createGoogleAccount({ username: values.username, preferredLanguage: locale });
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
  if (pending.data?.purpose !== "create_account") {
    return (
      <AuthShell title={t("auth.google.account.title")}>
        <div className="flex justify-center py-6">
          <Spinner label={t("auth.google.pending")} />
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title={t("auth.google.account.title")}
      description={t("auth.google.account.subtitle", { email: pending.data.email })}
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
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          autoFocus
          error={errors.username ? t(errors.username.message as TranslationKey) : undefined}
          {...register("username")}
        />

        <Button type="submit" disabled={isSubmitting}>
          {isSubmitting ? (
            <Spinner label={t("auth.common.submitting")} />
          ) : (
            t("auth.google.account.submit")
          )}
        </Button>
      </form>
    </AuthShell>
  );
}
