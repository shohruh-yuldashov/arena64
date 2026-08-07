import { useId, useState } from "react";

import { OTP_LENGTH } from "@/features/auth/model/otp";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";

/**
 * The six-digit code field — A64-021.5H §20.
 *
 * ## One input, not six
 *
 * §20 permits either and this takes the single field, because six
 * focus-jumping inputs are a well-known accessibility trap: a screen reader
 * announces six unlabelled boxes, backspace behaviour has to be
 * hand-written, and paste has to be intercepted and split. One field gets
 * all three from the platform.
 *
 * The visual segmentation people expect is `letter-spacing` and a monospace
 * face, which is presentation rather than structure — and therefore cannot
 * break the semantics.
 *
 * ## The three attributes that matter more than the styling
 *
 *     inputMode="numeric"          a phone keypad rather than a keyboard
 *     autoComplete="one-time-code" iOS and Android offer the code from the
 *                                  message; on iOS this is the *only* way
 *     maxLength                    typing past six is silently dropped
 *                                  rather than submitted and rejected
 *
 * ## No automatic submit
 *
 * §20 allows it "unless robust and tested", and it is not: a paste that
 * lands one character at a time fires a submit on the first six, and a
 * person who mistypes the last digit has spent an attempt before they can
 * correct it. Five attempts is not a budget to spend on a UX flourish.
 *
 * ## Digits only, on the way in
 *
 * Non-digits are stripped as the person types rather than rejected on
 * submit. The common case is a paste that carried a space or a zero-width
 * character out of a mail client, and refusing that would blame somebody
 * for their email client's formatting.
 */
export function OtpForm({
  onSubmit,
  submitting,
  error,
}: {
  onSubmit: (code: string) => void;
  submitting: boolean;
  error: TranslationKey | null;
}) {
  const { t } = useTranslation();
  const id = useId();
  const [code, setCode] = useState("");
  const complete = code.length === OTP_LENGTH;

  return (
    <form
      className="flex flex-col gap-4"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (complete && !submitting) onSubmit(code);
      }}
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor={id} className="text-sm font-medium">
          {t("auth.verifyEmail.codeLabel")}
        </label>
        <input
          id={id}
          value={code}
          onChange={(event) =>
            setCode(event.target.value.replace(/\D/g, "").slice(0, OTP_LENGTH))
          }
          disabled={submitting}
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={OTP_LENGTH}
          autoFocus
          // Both, always: `aria-describedby` carries the hint, and the
          // error is appended only when there is one — a reference to an
          // element that is not rendered is announced as nothing at all in
          // some screen readers and as an empty string in others.
          aria-describedby={error === null ? `${id}-hint` : `${id}-hint ${id}-error`}
          aria-invalid={error !== null}
          className="border-input focus-visible:border-ring focus-visible:ring-ring/50 min-h-11 w-full rounded-md border bg-transparent px-3 text-center font-mono text-2xl tracking-[0.4em] outline-none focus-visible:ring-[3px]"
        />
        <p id={`${id}-hint`} className="text-muted-foreground text-xs">
          {t("auth.verifyEmail.codeHint")}
        </p>
        {error !== null && (
          <p id={`${id}-error`} role="alert" className="text-destructive text-sm">
            {t(error)}
          </p>
        )}
      </div>

      <Button type="submit" disabled={!complete || submitting} className="min-h-11">
        {submitting && <Spinner label={t("auth.verifyEmail.submitting")} />}
        {submitting ? t("auth.verifyEmail.submitting") : t("auth.verifyEmail.submit")}
      </Button>
    </form>
  );
}
