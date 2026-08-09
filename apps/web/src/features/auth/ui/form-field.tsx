import { type ComponentProps, type ReactNode, useId } from "react";

import { PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH } from "@/features/auth/schemas";
import { useTranslation } from "@/shared/i18n";
import { Input } from "@/shared/ui";

/**
 * One labelled, described, error-aware field.
 *
 * ## Why this exists rather than a label and an input at each call site
 *
 * Four things have to agree for a field to be usable without sight: the
 * `<label htmlFor>` and the input's `id`, and the error's `id` and the
 * input's `aria-describedby`. Written by hand at every call site, one of
 * them is eventually wrong — and the symptom is invisible to everyone who
 * can see the red border. Generating the ids here makes them agree by
 * construction.
 *
 * ## The error is not only red
 *
 * WCAG 2.1 §1.4.1 — colour is never the sole indicator. The message is
 * text, it is announced by `role="alert"`, and `aria-invalid` marks the
 * control itself; the colour is the fourth signal, not the first.
 *
 * `noValidate` on the form plus `autoComplete` here rather than browser
 * validation: the native bubbles cannot be translated and cannot be styled,
 * and they announce differently in every browser.
 */
export function FormField({
  label,
  error,
  description,
  trailing,
  ...props
}: ComponentProps<typeof Input> & {
  label: string;
  /** A translation key's resolved text, or `undefined` when valid. */
  error?: string | undefined;
  description?: string | undefined;
  /**
   * A control inside the field's trailing edge — the password toggle, and
   * so far only that.
   *
   * A slot rather than a second component: the four ids above have to be
   * generated in one place or they stop agreeing, and a `PasswordField`
   * that copied them would be the fifth chance to get one wrong.
   */
  trailing?: ReactNode;
}) {
  const id = useId();
  const errorId = `${id}-error`;
  const descriptionId = `${id}-description`;

  const describedBy =
    [error !== undefined ? errorId : null, description !== undefined ? descriptionId : null]
      .filter(Boolean)
      .join(" ") || undefined;

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <div className="relative">
        <Input
          id={id}
          aria-invalid={error !== undefined}
          {...(describedBy !== undefined ? { "aria-describedby": describedBy } : {})}
          {...props}
          className={trailing !== undefined ? "pr-12" : undefined}
        />
        {trailing !== undefined && (
          <div className="absolute inset-y-0 right-0 flex items-center pr-1">{trailing}</div>
        )}
      </div>
      {description !== undefined && (
        <p id={descriptionId} className="text-muted-foreground text-xs">
          {description}
        </p>
      )}
      {error !== undefined && (
        <p id={errorId} role="alert" className="text-destructive text-sm font-medium">
          {error}
        </p>
      )}
    </div>
  );
}

/** The password policy, rendered before somebody submits rather than after. */
export function usePasswordHint(): string {
  const { t } = useTranslation();
  return t("auth.validation.passwordWeak") + ` (${PASSWORD_MIN_LENGTH}–${PASSWORD_MAX_LENGTH})`;
}
