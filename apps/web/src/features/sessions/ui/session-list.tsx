import { useState } from "react";

import { profileErrorKey } from "@/features/profile/model/error-messages";
import type { SessionRead } from "@/features/sessions/api";
import { useRevokeSession, useSessions } from "@/features/sessions/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import type { I18nContextValue } from "@/shared/i18n/context";
import { formatDate, formatRelativeTime } from "@/shared/lib/format";
import {
  Button,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  ListState,
  Notice,
  SettingCard,
  Spinner,
} from "@/shared/ui";

/**
 * The device list on `/settings/sessions` — A64-030.5C, SE-2.
 *
 * ## What a row is
 *
 * A **device**, not a session row. The backend rotates a browser's refresh
 * token roughly every fifteen minutes, replacing the row each time, so a
 * list of rows would renumber itself while somebody was looking at it. The
 * id here survives rotation; `browser_router.py` carries the reasoning.
 *
 * ## Why the current device has no button
 *
 * Not because the API refuses — it does not. Revoking this browser would
 * invalidate its cookie without clearing it, so the page would keep working
 * for up to fifteen minutes on the access token it already holds and then
 * drop to the login screen for no visible reason.
 *
 * "Sign out everywhere", below this list, is the control that ends this
 * session deliberately and navigates afterwards. One meaning per control.
 *
 * ## Why the label can say "Unknown"
 *
 * The server parses a user agent and returns `null` rather than guessing
 * when it does not recognise one. A confidently wrong "Firefox on Windows"
 * defeats the only purpose this screen has — letting somebody recognise
 * their own devices — more thoroughly than an honest "Unknown browser"
 * beside a platform and a timestamp that are both real.
 */
export function SessionList() {
  const { t, locale } = useTranslation();
  const sessions = useSessions();
  const revoke = useRevokeSession();

  //: The device awaiting confirmation, or `null`. One dialog for the list
  //: rather than one per row: a mounted dialog per device is N focus traps
  //: for an action taken at most once.
  const [pending, setPending] = useState<SessionRead | null>(null);
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  async function onConfirm(device: SessionRead): Promise<void> {
    setFailure(null);
    try {
      await revoke.mutateAsync(device.id);
      setPending(null);
    } catch (error) {
      // The dialog stays open on failure. Closing it would leave the row
      // in place with no explanation, which reads as the button doing
      // nothing rather than as the request having failed.
      setFailure(profileErrorKey(error));
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {failure !== null && pending === null && <Notice tone="error">{t(failure)}</Notice>}

      <ListState
        isPending={sessions.isPending}
        isError={sessions.isError}
        isEmpty={sessions.data?.length === 0}
        loadingLabel={t("profile.sessions.loading")}
        errorMessage={t("profile.sessions.loadFailed")}
        emptyTitle={t("profile.sessions.emptyTitle")}
        emptyHint={t("profile.sessions.emptyHint")}
        pendingRowClassName="h-20"
        onRetry={() => void sessions.refetch()}
      >
        <SettingCard>
          {/* Named, because the app shell is full of `<li>` too: a list
              without an accessible name is one a screen reader reaches with
              no idea what it is a list of. */}
          <ul aria-label={t("profile.sessions.title")}>
            {(sessions.data ?? []).map((device) => (
              <li
                key={device.id}
                // Stacked below `sm` for the same reason `SettingRow` is:
                // a device label, two lines of detail and a button do not
                // fit beside each other on a phone.
                className="border-border flex flex-col gap-3 border-b px-5 py-4 last:border-b-0 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex flex-col gap-0.5">
                  <span className="text-sm font-medium">{deviceLabel(device, t)}</span>
                  {device.is_current ? (
                    <span className="text-primary text-xs font-medium">
                      {t("profile.sessions.currentDevice")}
                    </span>
                  ) : (
                    <span className="text-muted-foreground text-xs">
                      {t("profile.sessions.lastActive", {
                        when: when(device.last_active_at, locale, t),
                      })}
                    </span>
                  )}
                  <span className="text-muted-foreground text-xs">
                    {t("profile.sessions.signedIn", {
                      when: when(device.signed_in_at, locale, t),
                    })}
                  </span>
                </div>

                {!device.is_current && (
                  <Button
                    variant="ghost"
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive min-h-11 sm:w-auto"
                    onClick={() => {
                      setFailure(null);
                      setPending(device);
                    }}
                  >
                    {t("profile.sessions.revoke")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </SettingCard>
      </ListState>

      <Dialog
        open={pending !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPending(null);
            setFailure(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("profile.sessions.revokeTitle")}</DialogTitle>
            <DialogDescription>
              {t("profile.sessions.revokeBody", {
                device: pending === null ? "" : deviceLabel(pending, t),
              })}
            </DialogDescription>
          </DialogHeader>

          {failure !== null && <Notice tone="error">{t(failure)}</Notice>}

          <div className="flex flex-wrap justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" className="min-h-11">
                {t("profile.sessions.cancel")}
              </Button>
            </DialogClose>
            <Button
              variant="destructive"
              className="min-h-11"
              disabled={revoke.isPending}
              onClick={() => {
                if (pending !== null) void onConfirm(pending);
              }}
            >
              {revoke.isPending ? (
                <Spinner label={t("profile.sessions.revoke")} />
              ) : (
                t("profile.sessions.revoke")
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

//: The context's own `t`, so this file cannot drift from its signature.
type Translate = I18nContextValue["t"];

/**
 * "Chrome on macOS", "Unknown browser on Windows", or "Unknown device".
 *
 * Composed here rather than sent as one string by the server, because the
 * word between the two halves is different in every locale this app ships
 * and "Chrome" and "macOS" are product names in all three.
 */
function deviceLabel(device: SessionRead, t: Translate): string {
  const { browser, platform } = device;
  if (
    browser !== null &&
    browser !== undefined &&
    platform !== null &&
    platform !== undefined
  ) {
    return t("profile.sessions.deviceOn", { browser, platform });
  }
  // A browser with no recognised platform reads better alone than paired
  // with a placeholder: "Chrome" beats "Chrome on unknown device".
  if (browser !== null && browser !== undefined) return browser;
  if (platform !== null && platform !== undefined) {
    return t("profile.sessions.deviceOn", {
      browser: t("profile.sessions.unknownBrowser"),
      platform,
    });
  }
  return t("profile.sessions.unknownDevice");
}

/**
 * "2 hours ago" while that is meaningful, the date once it is not.
 *
 * `formatRelativeTime` answers `null` beyond a week, deliberately — "four
 * months ago" is worse than the date it replaces.
 */
function when(iso: string, locale: string, t: Translate): string {
  return formatRelativeTime(iso, locale, t) ?? formatDate(iso, locale) ?? iso;
}
