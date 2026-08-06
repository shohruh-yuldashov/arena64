import { useQueryClient } from "@tanstack/react-query";
import { DownloadIcon, RefreshCwIcon, WifiOffIcon } from "lucide-react";
import type { ReactNode } from "react";

import { isAuthenticated } from "@/entities/session";
import { useSession } from "@/features/auth/model/session-provider";
import { useTranslation } from "@/shared/i18n";
import {
  applyAppUpdate,
  dismissAppUpdate,
  dismissInstall,
  isIosSafari,
  promptInstall,
  useAppUpdate,
  useAppUpdateHeld,
  useInstall,
  useOnline,
  useStandaloneDisplay,
} from "@/shared/pwa";
import { Button, Spinner } from "@/shared/ui";

/**
 * The three things a Progressive Web App has to say — A64-020.9 §15, §16,
 * §20, §23, §24.
 *
 * One strip, pinned to the bottom of every page by `AppShell`. Pinned
 * rather than inline because all three are interruptions to whatever the
 * player is doing, and none of them may push a board off the screen mid
 * game — §15's "does not block active Game UI" and §24's requirement that
 * this work at 360px.
 *
 * ## Why the container does not take pointer events
 *
 * A fixed strip across the bottom of the viewport would otherwise swallow
 * clicks on whatever is underneath it, including the row of game controls
 * that lives exactly there on a phone. `pointer-events-none` on the
 * container and `pointer-events-auto` on each card means the strip is only
 * "there" where something is actually drawn.
 *
 * ## Accessibility
 *
 * The update and offline cards are `role="status"` live regions: both
 * describe a change the user did not cause and needs to know about, and
 * `polite` means it is announced without interrupting. The install card is
 * **not** a live region — it is an offer, not an event, and announcing it
 * over whatever the user was reading is the manipulative timing §16
 * forbids, in audio.
 *
 * Nothing here is a modal: no focus trap, no focus steal, `Escape` does
 * nothing. Focus stays where the player put it, which is what lets the
 * update prompt sit visible through a whole game without being in the way.
 *
 * There is no animation, so `prefers-reduced-motion` has nothing to
 * suppress — the one exception is the spinner in the activating state,
 * which is `shared/ui`'s and already respects it.
 */
export function PwaNotices() {
  const { t } = useTranslation();

  return (
    <div
      // `pb-[env(safe-area-inset-bottom)]` — on a device with a home
      // indicator the strip would otherwise sit under it, where a tap
      // reaches the operating system instead of the button (§24).
      className="pointer-events-none fixed inset-x-0 bottom-0 z-40 flex flex-col items-center gap-2 p-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))]"
      aria-label={t("pwa.region")}
    >
      <OfflineNotice />
      <AppUpdateNotice />
      <InstallNotice />
    </div>
  );
}

/** The shared card. Bounded width so a desktop does not get a full-width bar. */
function Notice({ children, ...props }: { children: ReactNode } & React.ComponentProps<"div">) {
  return (
    <div
      className="bg-card text-card-foreground pointer-events-auto flex w-full max-w-md flex-col gap-3 rounded-lg border p-4 shadow-lg sm:flex-row sm:items-center sm:justify-between"
      {...props}
    >
      {children}
    </div>
  );
}

/**
 * "You are offline" — a hint, and labelled as one.
 *
 * Shown only when the browser says there is no network at all, which is
 * the one direction `navigator.onLine` is trustworthy in (§20). It never
 * claims a game is or is not running: the socket's own status says that,
 * on the game screen, where it belongs.
 *
 * The retry re-runs the queries that are currently mounted rather than
 * reloading the page. A reload during an offline session would throw away
 * every cache TanStack Query is holding and replace a stale screen with an
 * empty one.
 */
function OfflineNotice() {
  const { t } = useTranslation();
  const online = useOnline();
  const queryClient = useQueryClient();

  if (online) return null;

  return (
    <Notice role="status" aria-live="polite">
      <div className="flex items-start gap-3">
        <WifiOffIcon aria-hidden="true" className="mt-0.5 size-5 shrink-0" />
        <div>
          <p className="text-sm font-medium">{t("pwa.offline.title")}</p>
          <p className="text-muted-foreground text-sm">{t("pwa.offline.description")}</p>
        </div>
      </div>
      <Button
        size="sm"
        variant="outline"
        className="min-h-11 shrink-0"
        onClick={() => void queryClient.refetchQueries({ type: "active" })}
      >
        {t("pwa.offline.retry")}
      </Button>
    </Notice>
  );
}

/**
 * "A new version is ready" — and the button that is deliberately missing
 * sometimes.
 *
 * Three states, and the middle one is the point of §14:
 *
 *     available + held      says so, offers nothing. A reload here costs a
 *                           game, an offer, or a mutation nobody sees land
 *     available             Update / Later
 *     activating            the worker is taking over; the reload it
 *                           causes is the only one this app ever performs
 *
 * `dismissed` hides it entirely until a *different* worker arrives, which
 * is what stops "Later" from being a question asked again on every render
 * and every route change (§15).
 */
function AppUpdateNotice() {
  const { t } = useTranslation();
  const update = useAppUpdate();
  const held = useAppUpdateHeld();

  if (update.status === "idle") return null;
  if (update.status === "available" && update.dismissed) return null;

  if (update.status === "activating") {
    return (
      <Notice role="status" aria-live="polite">
        <div className="flex items-center gap-3">
          <Spinner label={t("pwa.update.activating")} />
          <p className="text-sm font-medium">{t("pwa.update.activating")}</p>
        </div>
      </Notice>
    );
  }

  if (held) {
    return (
      <Notice role="status" aria-live="polite">
        <div className="flex items-start gap-3">
          <RefreshCwIcon aria-hidden="true" className="mt-0.5 size-5 shrink-0" />
          <div>
            <p className="text-sm font-medium">{t("pwa.update.title")}</p>
            <p className="text-muted-foreground text-sm">{t("pwa.update.held")}</p>
          </div>
        </div>
      </Notice>
    );
  }

  return (
    <Notice role="status" aria-live="polite">
      <div className="flex items-start gap-3">
        <RefreshCwIcon aria-hidden="true" className="mt-0.5 size-5 shrink-0" />
        <div>
          <p className="text-sm font-medium">{t("pwa.update.title")}</p>
          <p className="text-muted-foreground text-sm">{t("pwa.update.description")}</p>
        </div>
      </div>
      <div className="flex shrink-0 gap-2">
        <Button size="sm" className="min-h-11" onClick={() => applyAppUpdate()}>
          {t("pwa.update.action")}
        </Button>
        <Button size="sm" variant="ghost" className="min-h-11" onClick={dismissAppUpdate}>
          {t("pwa.update.later")}
        </Button>
      </div>
    </Notice>
  );
}

/**
 * "Install Arena64" — offered once the player has a reason to want it.
 *
 * ## The engagement trigger is the session
 *
 * §16 forbids prompting on first paint and suggests "after successful
 * login" as a trigger. That is this condition, and it is the honest one:
 * an installed Arena64 is a shortcut to a game, which is worth nothing to
 * a visitor who has not signed in. The explicit entry §16 also asks for
 * lives on `/settings/preferences`, where it is available whether or not
 * this bar was ever dismissed.
 *
 * ## iOS gets words, not a button
 *
 * Safari has no `beforeinstallprompt`, so there is nothing to trigger and
 * this says so plainly rather than rendering a button that would do
 * nothing (§17).
 */
function InstallNotice() {
  const { t } = useTranslation();
  const { state } = useSession();
  const install = useInstall();
  const standalone = useStandaloneDisplay();

  const ios = isIosSafari();
  const eligible =
    isAuthenticated(state) &&
    !standalone &&
    !install.installed &&
    !install.dismissed &&
    (install.canPrompt || ios);

  if (!eligible) return null;

  return (
    <Notice aria-labelledby="pwa-install-title">
      <div className="flex items-start gap-3">
        <DownloadIcon aria-hidden="true" className="mt-0.5 size-5 shrink-0" />
        <div>
          <p id="pwa-install-title" className="text-sm font-medium">
            {t(ios && !install.canPrompt ? "pwa.install.iosTitle" : "pwa.install.title")}
          </p>
          <p className="text-muted-foreground text-sm">
            {t(ios && !install.canPrompt ? "pwa.install.iosSteps" : "pwa.install.description")}
          </p>
        </div>
      </div>
      <div className="flex shrink-0 gap-2">
        {install.canPrompt && (
          <Button size="sm" className="min-h-11" onClick={() => void promptInstall()}>
            {t("pwa.install.action")}
          </Button>
        )}
        <Button size="sm" variant="ghost" className="min-h-11" onClick={dismissInstall}>
          {t("pwa.install.later")}
        </Button>
      </div>
    </Notice>
  );
}
