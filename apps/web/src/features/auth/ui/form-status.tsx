import { type ReactNode, useEffect, useRef } from "react";

import { type TranslationKey, useTranslation } from "@/shared/i18n";

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
 * `role="alert"` is an assertive live region: it interrupts, which is
 * correct for "your submission failed" and would be wrong for a status
 * update. `FormStatus` below uses `role="status"`, which waits for a pause.
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
    <div
      ref={ref}
      role="alert"
      tabIndex={-1}
      className="border-destructive/50 bg-destructive/10 text-destructive rounded-md border px-3 py-2 text-sm font-medium"
    >
      {t(messageKey)}
    </div>
  );
}

/** A non-interrupting confirmation — "we sent the link", "that worked". */
export function FormStatus({ children }: { children: ReactNode }) {
  return (
    <div
      role="status"
      className="border-border bg-muted/40 text-foreground rounded-md border px-3 py-2 text-sm"
    >
      {children}
    </div>
  );
}
