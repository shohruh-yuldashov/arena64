import { CheckIcon, Share2Icon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { track } from "@/shared/analytics";
import { useTranslation } from "@/shared/i18n";
import { reportError } from "@/shared/lib/report-error";
import { Button } from "@/shared/ui/button";

/**
 * Hand somebody the address of the page they are on — A64-026.4 §43.8.
 *
 * ## Why a control at all, when the URL is in the address bar
 *
 * On a phone it is not, or not usefully: the bar collapses while scrolling,
 * selecting a URL by touch is fiddly, and the share sheet is where the
 * destination actually is. This is the control that exists on the pages a
 * person is expected to send to somebody else.
 *
 * ## Two mechanisms, in the order a platform prefers
 *
 * `navigator.share` opens the system sheet, which is the point on a phone
 * and is unavailable on most desktop browsers. `navigator.clipboard` is the
 * desktop answer. Neither is universal, so the choice is made per call
 * rather than once.
 *
 * ## And nothing when neither exists
 *
 * Both need a secure context, and the clipboard can be absent or refused
 * outright. A button that cannot do its one job is worse than no button —
 * it looks broken, and the URL is genuinely in the address bar on any
 * browser old enough to lack both. So it renders `null`.
 *
 * Detection happens in an effect rather than during render: reading
 * `navigator` while rendering makes this component's output depend on the
 * environment rather than on its props.
 *
 * ## Cancelling is not failing
 *
 * Dismissing the system sheet rejects with `AbortError`. Reporting that
 * would fill a log with people changing their minds, so it is the one
 * rejection swallowed deliberately — the only one, and named.
 */
export function ShareButton({ title, className }: { title: string; className?: string }) {
  const { t } = useTranslation();
  const [available, setAvailable] = useState(false);
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setAvailable(canShare() || canCopy());
  }, []);

  // The confirmation clears itself, and the timer is cleared on unmount so
  // a state update never lands on a component that has gone.
  useEffect(() => () => clearTimer(timer), []);

  if (!available) return null;

  const share = async () => {
    const url = window.location.href;

    if (canShare()) {
      try {
        await navigator.share({ title, url });
        // A64-027.2 §38. **After it succeeded**, and never the URL: the
        // taxonomy's properties are the surface and the mechanism, so the
        // two paths can be compared without storing what was shared.
        track("share_clicked", { surface: "tournament", mechanism: "share_sheet" });
        return;
      } catch (error) {
        if (isAbort(error)) return;
        // Not fatal: the sheet failed, the clipboard may still work, and
        // somebody who pressed Share would rather have the link than an
        // explanation of why they do not.
        reportError(error, { scope: "share" });
      }
    }

    if (!canCopy()) return;

    try {
      await navigator.clipboard.writeText(url);
      track("share_clicked", { surface: "tournament", mechanism: "clipboard" });
      setCopied(true);
      clearTimer(timer);
      timer.current = setTimeout(() => setCopied(false), CONFIRMATION_MS);
    } catch (error) {
      reportError(error, { scope: "share" });
    }
  };

  return (
    <>
      <Button variant="outline" size="sm" className={className} onClick={() => void share()}>
        {copied ? (
          <CheckIcon aria-hidden="true" className="size-4" />
        ) : (
          <Share2Icon aria-hidden="true" className="size-4" />
        )}
        {copied ? t("share.copied") : t("share.action")}
      </Button>

      {/* The label change is visible; this is the same fact for somebody who
          is not looking at it. `role="status"` announces politely, which is
          right for a confirmation nobody is waiting on. */}
      <span role="status" className="sr-only">
        {copied ? t("share.copied") : ""}
      </span>
    </>
  );
}

const CONFIRMATION_MS = 2000;

function canShare(): boolean {
  return typeof navigator !== "undefined" && typeof navigator.share === "function";
}

function canCopy(): boolean {
  return (
    typeof navigator !== "undefined" && typeof navigator.clipboard?.writeText === "function"
  );
}

/** The reader closed the sheet. A decision, not a failure. */
function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function clearTimer(timer: { current: ReturnType<typeof setTimeout> | null }): void {
  if (timer.current !== null) clearTimeout(timer.current);
  timer.current = null;
}
