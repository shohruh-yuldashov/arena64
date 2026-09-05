import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { useTranslation } from "@/shared/i18n";
import { Icon } from "@/shared/ui/icon";

/**
 * Action feedback — A64-027A §29.
 *
 * Before this task an administrator who applied a sanction learned that it
 * worked because the row changed. That is inference, not feedback, and it
 * fails exactly when it matters: an action that silently did nothing looks
 * the same as one whose effect is off-screen.
 *
 * ## Announced, not merely drawn
 *
 * The region is `aria-live="polite"`, so the sentence reaches a screen
 * reader without stealing focus. `role="status"` rather than `alert` even
 * for failures: an alert interrupts, and a failed request the operator can
 * see in place does not warrant interrupting them mid-sentence. Errors that
 * genuinely need interrupting are rendered inline by the page, as
 * `ErrorState`.
 *
 * ## Dismissal is manual as well as automatic
 *
 * Auto-dismiss alone is a trap for anyone reading slowly, and a permanent
 * toast is a trap for everyone else. Both, therefore — and the timer is
 * cleared on unmount so a toast raised by a page that then navigates away
 * cannot set state on a dead component.
 */

export type ToastTone = "success" | "danger" | "info";

interface Toast {
  id: number;
  tone: ToastTone;
  message: string;
}

interface ToastApi {
  notify: (message: string, tone?: ToastTone) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const DISMISS_AFTER_MS = 6000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const notify = useCallback(
    (message: string, tone: ToastTone = "success") => {
      const id = nextId.current++;
      setToasts((current) => [...current, { id, tone, message }]);
      timers.current.set(
        id,
        setTimeout(() => {
          dismiss(id);
        }, DISMISS_AFTER_MS),
      );
    },
    [dismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) clearTimeout(timer);
      pending.clear();
    };
  }, []);

  const api = useMemo(() => ({ notify }), [notify]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className="toast" data-tone={toast.tone}>
            <span className="toast__glyph" data-tone={toast.tone}>
              <Icon
                name={
                  toast.tone === "success"
                    ? "success"
                    : toast.tone === "danger"
                      ? "warning"
                      : "info"
                }
                size={17}
              />
            </span>
            <span>{toast.message}</span>
            <button
              type="button"
              onClick={() => {
                dismiss(toast.id);
              }}
              aria-label={t("state.dismiss")}
            >
              <Icon name="close" size={15} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/**
 * Returns a no-op outside a provider rather than throwing.
 *
 * A page rendered in a unit test without the shell should exercise its own
 * behaviour, not fail on the absence of a notification surface it does not
 * assert on.
 */
export function useToast(): ToastApi {
  return useContext(ToastContext) ?? { notify: () => undefined };
}
