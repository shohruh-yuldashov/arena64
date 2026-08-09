import { type ReactNode, useEffect, useId, useRef } from "react";

import { useTranslation } from "@/shared/i18n";

/**
 * A modal confirmation, on the platform's own `<dialog>` — A64-024.6 §21.
 *
 * **Not `window.confirm`.** That dialog cannot carry a form, cannot be
 * styled, cannot be localised, and on some platforms can be suppressed by
 * the browser — which would turn a security-sensitive confirmation into no
 * confirmation at all.
 *
 * **Not a dependency either.** `<dialog showModal()>` already gives the
 * three properties that make a modal accessible, implemented by the
 * browser rather than by us: a focus trap, `Escape` to dismiss, and
 * inertness of the page behind it. A library would be several hundred
 * kilobytes to re-implement what the element does natively — and this app
 * has exactly one modal.
 *
 * ## Focus returns to where it came from
 *
 * `close()` restores focus to the element that opened the dialog, which
 * the browser does for `showModal()`. The one thing it does **not** do is
 * move focus into the dialog on a re-render, so the first control is
 * focused explicitly on open — otherwise a keyboard operator would be
 * tabbing from the page behind an invisible barrier.
 *
 * ## Why the confirm button is not the default
 *
 * `Enter` in the form submits, and the submit button is the confirm — but
 * the dialog opens with focus on the **first** control, which is the
 * reason field for a restriction. An operator who opens the dialog by
 * accident and presses `Escape` cancels; one who presses `Enter` with an
 * empty required field is stopped by the browser's own validation.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  busy,
  error,
  onConfirm,
  onCancel,
  children,
}: {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const element = dialog.current;
    if (element === null) return;

    if (open && !element.open) {
      element.showModal();
      // The browser traps focus but does not place it; without this the
      // first Tab would land on the dialog's own close affordance rather
      // than on the field the operator has to fill in.
      element.querySelector<HTMLElement>("input, select, textarea, button")?.focus();
    }
    if (!open && element.open) element.close();
  }, [open]);

  return (
    <dialog
      ref={dialog}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      // `Escape` closes the element natively; this keeps our state in step
      // with what the browser already did.
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <form
        method="dialog"
        onSubmit={(event) => {
          event.preventDefault();
          onConfirm();
        }}
      >
        <h3 id={titleId}>{title}</h3>
        <p id={descriptionId}>{description}</p>

        {children}

        {error !== null && (
          <p role="alert" className="error">
            {error}
          </p>
        )}

        <p className="dialog-actions">
          <button type="button" onClick={onCancel} disabled={busy}>
            {t("moderation.cancel")}
          </button>
          {/* Named for what it does, not "OK" — §27: a destructive action
              must say which action it is. */}
          <button type="submit" className="action danger" disabled={busy}>
            {busy ? t("moderation.working") : confirmLabel}
          </button>
        </p>
      </form>
    </dialog>
  );
}
