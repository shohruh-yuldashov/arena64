import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";

import { resendVerification, verifyEmail } from "@/features/auth/api";
import { messageKeyFor } from "@/features/auth/model/error-messages";
import {
  resendVerificationSchema,
  type ResendVerificationValues,
} from "@/features/auth/schemas";
import { FormField } from "@/features/auth/ui/form-field";
import { FormError, FormStatus } from "@/features/auth/ui/form-status";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";
import { AuthShell } from "@/widgets/auth-shell";

/**
 * The page a verification link lands on, and the way to ask for another.
 *
 * ## Four states, one of which is not an error
 *
 *     no token      the link was truncated by a mail client
 *     verifying     the exchange is in flight
 *     verified      it worked, or it had already worked
 *     failed        expired, already used, or never valid
 *
 * "Already verified" is deliberately **not** distinguished from "just
 * verified": the backend answers the same for both, the user's situation is
 * identical, and telling them their link was stale after it worked is a
 * distinction that only creates doubt.
 *
 * ## Why the resend form does not say whether the address exists
 *
 * `POST /auth/email/resend` answers neutrally by design, and this repeats
 * the same neutral sentence for every non-fault outcome. A form that said
 * "no account with that address" would be an enumeration oracle attached to
 * an unauthenticated endpoint.
 *
 * ## Strict Mode
 *
 * The token is single-use. Two exchanges means the second fails and the
 * page reports failure for a link that worked — so the effect is guarded by
 * a ref, exactly as the session bootstrap is.
 */
export default function VerifyEmailPage() {
  const { t } = useTranslation();
  const { token } = useSearch({ from: "/verify-email" });
  const attempted = useRef(false);
  const [outcome, setOutcome] = useState<"idle" | "verifying" | "verified" | "failed">(
    token !== undefined && token !== "" ? "verifying" : "idle",
  );

  useEffect(() => {
    if (token === undefined || token === "" || attempted.current) return;
    attempted.current = true;

    verifyEmail({ token })
      .then(() => setOutcome("verified"))
      .catch(() => setOutcome("failed"));
  }, [token]);

  return (
    <AuthShell
      title={t("auth.verifyEmail.title")}
      footer={
        <Link to="/login" className="text-foreground underline underline-offset-4">
          {t("auth.verifyEmail.toLogin")}
        </Link>
      }
    >
      {outcome === "idle" && <FormStatus>{t("auth.verifyEmail.missingToken")}</FormStatus>}

      {outcome === "verifying" && (
        <div className="flex items-center gap-2">
          <Spinner label={t("auth.verifyEmail.verifying")} />
          <span className="text-muted-foreground text-sm">
            {t("auth.verifyEmail.verifying")}
          </span>
        </div>
      )}

      {outcome === "verified" && (
        <FormStatus>
          <strong className="block font-medium">{t("auth.verifyEmail.successTitle")}</strong>
          {t("auth.verifyEmail.successBody")}
        </FormStatus>
      )}

      {outcome === "failed" && (
        <>
          <div role="alert" className="text-destructive text-sm font-medium">
            <strong className="block">{t("auth.verifyEmail.invalidTitle")}</strong>
            {t("auth.verifyEmail.invalidBody")}
          </div>
          <ResendForm />
        </>
      )}

      {outcome === "idle" && <ResendForm />}
    </AuthShell>
  );
}

function ResendForm() {
  const { t } = useTranslation();
  const [sent, setSent] = useState(false);
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<ResendVerificationValues>({
    resolver: zodResolver(resendVerificationSchema),
    mode: "onTouched",
    defaultValues: { email: "" },
  });

  // A mutation rather than local state, so the retry policy and the global
  // error handler are the app's rather than this component's.
  const resend = useMutation({
    mutationFn: resendVerification,
    onSuccess: () => {
      setSent(true);
      setFailure(null);
    },
    onError: (error) => setFailure(messageKeyFor(error)),
  });

  const onSubmit = handleSubmit((values) => resend.mutate(values));

  if (sent) {
    return <FormStatus>{t("auth.verifyEmail.resendSent")}</FormStatus>;
  }

  return (
    <form onSubmit={(event) => void onSubmit(event)} className="flex flex-col gap-3" noValidate>
      <h2 className="text-sm font-medium">{t("auth.verifyEmail.resendTitle")}</h2>
      <FormError messageKey={failure} />
      <FormField
        label={t("auth.common.email")}
        type="email"
        autoComplete="email"
        error={errors.email ? t(errors.email.message as TranslationKey) : undefined}
        {...register("email")}
      />
      <Button type="submit" variant="outline" disabled={resend.isPending}>
        {resend.isPending ? (
          <Spinner label={t("auth.common.submitting")} />
        ) : (
          t("auth.verifyEmail.resend")
        )}
      </Button>
    </form>
  );
}
