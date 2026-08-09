import { type ReactNode, useEffect, useRef } from "react";

import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Notice } from "@/shared/ui";

/**
 * The form-level error, announced and focused.
 *
 * ## Why focus moves here
 *
 * A failed submit that only paints a message leaves a screen-reader user
 * where they were — at the bottom of a form that now says something they
 * cannot see. WCAG 2.1 §3.3.1 wants the error identified; moving focus to
 * it is what makes the identification reach them.
 *
 * `tabIndex={-1}` makes the element programmatically focusable without
 * putting it in the tab order, so keyboard users are not made to tab past
 * an empty region on every subsequent pass.
 *
 * ## The tint is `Notice`'s now — A64-025.4 §12
 *
 * The border, the background and the assertive role came from a class
 * string written here, and A64-025.2 then shipped `Notice` with exactly
 * those four tones. Two components disagreeing about what a failure looks
 * like is the duplication that task existed to end, and this one predates
 * it — so the shape comes from the shared primitive and what stays here is
 * the one thing `Notice` does not do: move focus.
 *
 * `Notice` picks `role="alert"` from `tone="error"` on its own, so nothing
 * here states it.
 */
export function FormError({ messageKey }: { messageKey: TranslationKey | null }) {
  const ref = useRef<HTMLDivElement>(null);
  const { t } = useTranslation();

  useEffect(() => {
    if (messageKey !== null) {
      ref.current?.focus();
    }
  }, [messageKey]);

  if (messageKey === null) return null;

  return (
    <Notice ref={ref} tone="error" tabIndex={-1} className="font-medium">
      {t(messageKey)}
    </Notice>
  );
}

/**
 * A non-interrupting confirmation — "we sent the link", "that worked".
 *
 * Either tone is `role="status"`: it waits for a pause rather than
 * interrupting, because nothing here has failed.
 *
 * `success` is for the end of a journey — the address is verified, the
 * password is changed. `info` is for everything still in progress, which is
 * most of them: "we sent the link" is not an outcome, it is a step. Marking
 * both the same way is how a person stops noticing which is which.
 */
export function FormStatus({
  tone = "info",
  children,
}: {
  tone?: "info" | "success";
  children: ReactNode;
}) {
  return <Notice tone={tone}>{children}</Notice>;
}
