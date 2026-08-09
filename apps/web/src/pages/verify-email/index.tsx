import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";

import { isAuthenticated } from "@/entities/session";
import {
  resendVerification,
  resendVerificationCode,
  verifyEmail,
  verifyEmailCode,
} from "@/features/auth/api";
import { messageKeyFor } from "@/features/auth/model/error-messages";
import { cooldownFrom, maskEmail, otpErrorKey } from "@/features/auth/model/otp";
import { safeRedirect } from "@/features/auth/model/safe-redirect";
import { useSession } from "@/features/auth/model/session-provider";
import {
  resendVerificationSchema,
  type ResendVerificationValues,
} from "@/features/auth/schemas";
import { FormField } from "@/features/auth/ui/form-field";
import { FormError, FormStatus } from "@/features/auth/ui/form-status";
import { OtpForm } from "@/features/auth/ui/otp-form";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";
import { AuthShell } from "@/widgets/auth-shell";

/**
 * Confirming an email address — A64-021.5H §17, §22, §23.
 *
 * ## One path, two credentials
 *
 * A `?token=` in the URL means somebody followed a **link** from an older
 * email, and that half is unchanged and unauthenticated: the person may be
 * reading mail in a browser they have never signed in on, and bouncing them
 * to `/login` would strand a one-time token they cannot re-request easily.
 *
 * Without a token this is the **code** screen, and it needs a session —
 * `POST /auth/email/verify-code` takes no address, because the session is
 * what says whose challenge it is.
 *
 * Both end at the same `is_verified`, which is what §13 means by
 * converging: one row, one rule, and a code that succeeds also ends any
 * live link.
 *
 * ## Nothing here is the authority
 *
 * §22. The verified state comes from the session's `user`, the validity of
 * a code from the server, and the cooldown from `Retry-After`. Reloading
 * works because none of those live in this component — what a reload loses
 * is a half-typed field, which is the correct thing to lose.
 *
 * ## Already verified is not an error
 *
 * §23. A person whose account was verified in another tab, or by a link
 * they clicked in their mail app, arrives here and is sent on. The backend
 * answers a submitted code with success for the same reason.
 */
export default function VerifyEmailPage() {
  const { token } = useSearch({ from: "/verify-email" });

  // The link half first, and deliberately outside the session check: it is
  // the backward-compatible path and must work for a signed-out visitor.
  if (token !== undefined && token !== "") {
    return <LinkVerification token={token} />;
  }
  return <CodeVerification />;
}

/** The pre-A64-021.5H flow, kept for links already in inboxes — §13. */
function LinkVerification({ token }: { token: string }) {
  const { t } = useTranslation();
  const attempted = useRef(false);
  const [outcome, setOutcome] = useState<"verifying" | "verified" | "failed">("verifying");

  useEffect(() => {
    // The token is single-use, and Strict Mode renders effects twice: a
    // second exchange fails and would report failure for a link that
    // worked. Guarded by a ref, exactly as the session bootstrap is.
    if (attempted.current) return;
    attempted.current = true;
    verifyEmail({ token })
      .then(() => setOutcome("verified"))
      .catch(() => setOutcome("failed"));
  }, [token]);

  return (
    <AuthShell title={t("auth.verifyEmail.title")} footer={<ToLogin />}>
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
          <ResendLinkForm />
        </>
      )}
    </AuthShell>
  );
}

/** The primary flow: six digits, for a signed-in unverified account. */
function CodeVerification() {
  const { t } = useTranslation();
  const { state, applyUser } = useSession();
  const navigate = useNavigate();
  const { next } = useSearch({ from: "/verify-email" });
  const [error, setError] = useState<TranslationKey | null>(null);
  const [resent, setResent] = useState(false);
  const [cooldown, setCooldown] = useState(0);

  const verified = isAuthenticated(state) && state.user.is_verified;

  useEffect(() => {
    // §23. Verified elsewhere — another tab, or a link in a mail app — so
    // there is nothing to do here but leave. `replace`, so Back does not
    // return to a screen that has already served its purpose.
    if (!verified) return;
    void navigate({ to: safeRedirect(next), replace: true });
  }, [navigate, next, verified]);

  useEffect(() => {
    // Presentation only. The server refuses a resend inside its own window
    // whatever this says, and this only ever counts **down** — a client
    // that could clear it early would be offering a button that fails.
    if (cooldown <= 0) return;
    const timer = window.setTimeout(() => setCooldown((left) => left - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  const verify = useMutation({
    mutationFn: verifyEmailCode,
    onSuccess: (user) => {
      setError(null);
      // The server's own answer, applied to the session — which is what
      // makes the guard, the header and every page agree without a second
      // request. §26: a client cannot forge this.
      applyUser(user);
      void navigate({ to: safeRedirect(next), replace: true });
    },
    onError: (failure) => setError(otpErrorKey(failure)),
  });

  const resend = useMutation({
    mutationFn: resendVerificationCode,
    onSuccess: () => {
      setResent(true);
      setError(null);
      setCooldown(60);
    },
    onError: (failure) => {
      setError(otpErrorKey(failure));
      // The number the **server** gave. Never invented — §11.
      setCooldown(cooldownFrom(failure));
    },
  });

  if (!isAuthenticated(state)) {
    // No session and no token: nothing to verify and nobody to verify it
    // for. `RequireAuth` is not used here because the link half above must
    // stay anonymous, so this is the same answer for the other half.
    return (
      <AuthShell title={t("auth.verifyEmail.title")} footer={<ToLogin />}>
        <FormStatus>{t("auth.verifyEmail.missingToken")}</FormStatus>
        <ResendLinkForm />
      </AuthShell>
    );
  }

  if (verified) {
    return (
      <AuthShell title={t("auth.verifyEmail.verifiedTitle")} footer={<ToLogin />}>
        <FormStatus tone="success">{t("auth.verifyEmail.verifiedBody")}</FormStatus>
      </AuthShell>
    );
  }

  return (
    <AuthShell title={t("auth.verifyEmail.codeTitle")} footer={<ToLogin />}>
      <p className="text-muted-foreground text-sm">
        {/* Masked — §21. Enough to recognise which address it went to, and
            not enough to publish one on a screen somebody may be sharing. */}
        {t("auth.verifyEmail.codeIntro", { email: maskEmail(state.user.email) })}
      </p>

      <OtpForm
        onSubmit={(code) => verify.mutate({ code })}
        submitting={verify.isPending}
        error={error}
      />

      {resent && !verify.isPending && (
        <FormStatus>{t("auth.verifyEmail.resendCodeSent")}</FormStatus>
      )}

      <Button
        type="button"
        variant="ghost"
        className="min-h-11"
        disabled={cooldown > 0 || resend.isPending}
        onClick={() => resend.mutate()}
      >
        {cooldown > 0
          ? t("auth.verifyEmail.resendCodeIn", { seconds: cooldown })
          : t("auth.verifyEmail.resendCode")}
      </Button>
    </AuthShell>
  );
}

function ToLogin() {
  const { t } = useTranslation();
  return (
    <Link to="/login" className="text-foreground underline underline-offset-4">
      {t("auth.verifyEmail.toLogin")}
    </Link>
  );
}

/**
 * The anonymous escape hatch, unchanged — §13.
 *
 * Still asks for an address and still issues a **link**, because its caller
 * has no session to submit a code with. The reply is deliberately identical
 * whatever happened: this endpoint is unauthenticated, so a form that said
 * "no account with that address" would be an enumeration oracle.
 */
function ResendLinkForm() {
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

  const resend = useMutation({
    mutationFn: resendVerification,
    onSuccess: () => {
      setSent(true);
      setFailure(null);
    },
    onError: (error) => setFailure(messageKeyFor(error)),
  });

  const onSubmit = handleSubmit((values) => resend.mutate(values));

  if (sent) return <FormStatus>{t("auth.verifyEmail.resendSent")}</FormStatus>;

  return (
    <form onSubmit={(event) => void onSubmit(event)} className="flex flex-col gap-3" noValidate>
      <h2 className="text-sm font-medium">{t("auth.verifyEmail.resendTitle")}</h2>
      <FormError messageKey={failure} />
      <FormField
        label={t("auth.common.email")}
        type="email"
        autoComplete="email"
        error={errors.email?.message}
        {...register("email")}
      />
      <Button type="submit" disabled={resend.isPending} className="min-h-11">
        {resend.isPending && <Spinner label={t("auth.verifyEmail.resend")} />}
        {t("auth.verifyEmail.resend")}
      </Button>
    </form>
  );
}
