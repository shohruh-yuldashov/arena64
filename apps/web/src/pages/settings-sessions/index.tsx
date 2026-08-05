import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { useSession } from "@/features/auth/model/session-provider";
import { FormError } from "@/features/auth/ui/form-status";
import { profileErrorKey } from "@/features/profile/model/error-messages";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import {
  Button,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
  Spinner,
} from "@/shared/ui";
import { SettingsShell } from "@/widgets/settings-shell";

/**
 * `/settings/sessions` — one action, and an honest note about the rest.
 *
 * ## Why there is no device list
 *
 * `SessionService.list_user_sessions` exists in the backend and **no HTTP
 * endpoint exposes it**. So there is nothing to list, and this page says so
 * rather than rendering an empty table that looks broken or — worse —
 * inventing rows from the current session, which would be a device list
 * with exactly one entry that is always "this one".
 *
 * Publishing that endpoint is a backend change with its own visibility
 * questions (what a session row may reveal about an IP and a user agent),
 * and A64-020.3 is a frontend phase. Recorded as deferred in
 * `specs/frontend.md`.
 *
 * ## "Sign out everywhere" is destructive and behaves like it
 *
 * It revokes **every** session including this one, so it asks first —
 * through Radix's dialog, which traps focus, returns it, and closes on
 * `Escape`. Afterwards the app is anonymous, so it navigates to `/login`;
 * staying on a `RequireAuth` route would bounce through the guard and land
 * there anyway, with a flash of the redirect on the way.
 *
 * The private query cache is cleared by `signOutEverywhere` itself, in the
 * auth layer — this page does not repeat it, because two places that clear
 * the cache is one place that can be forgotten.
 */
export default function SettingsSessionsPage() {
  const { t } = useTranslation();
  const { signOutEverywhere } = useSession();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<TranslationKey | null>(null);

  async function onConfirm(): Promise<void> {
    setPending(true);
    setFailure(null);
    try {
      await signOutEverywhere();
      setOpen(false);
      await navigate({ to: "/login", replace: true });
    } catch (error) {
      // `signOutEverywhere` clears this device in a `finally`, so the user
      // is already signed out locally; this only reports that the server
      // call did not land.
      setFailure(profileErrorKey(error));
    } finally {
      setPending(false);
    }
  }

  return (
    <SettingsShell
      title={t("profile.sessions.title")}
      description={t("profile.sessions.subtitle")}
    >
      <div className="flex flex-col gap-4">
        <FormError messageKey={failure} />

        <p className="text-muted-foreground text-sm">{t("profile.sessions.listDeferred")}</p>

        <div className="border-border flex flex-col gap-2 rounded-md border p-4">
          <h2 className="text-sm font-medium">{t("profile.sessions.signOutAll")}</h2>
          <p className="text-muted-foreground text-xs">
            {t("profile.sessions.signOutAllHint")}
          </p>

          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button variant="destructive" className="mt-2 min-h-11 self-start">
                {t("profile.sessions.signOutAll")}
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>{t("profile.sessions.confirmTitle")}</DialogTitle>
                <DialogDescription>{t("profile.sessions.confirmBody")}</DialogDescription>
              </DialogHeader>
              <div className="flex flex-wrap justify-end gap-2">
                <DialogClose asChild>
                  <Button variant="ghost" className="min-h-11">
                    {t("profile.sessions.cancel")}
                  </Button>
                </DialogClose>
                <Button
                  variant="destructive"
                  className="min-h-11"
                  disabled={pending}
                  onClick={() => void onConfirm()}
                >
                  {pending ? (
                    <Spinner label={t("profile.sessions.confirm")} />
                  ) : (
                    t("profile.sessions.confirm")
                  )}
                </Button>
              </div>
            </DialogContent>
          </Dialog>
        </div>
      </div>
    </SettingsShell>
  );
}
