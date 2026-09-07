import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { useSession } from "@/features/auth/model/session-provider";
import { FormError } from "@/features/auth/ui/form-status";
import { profileErrorKey } from "@/features/profile/model/error-messages";
import { SessionList } from "@/features/sessions";
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
  SettingCard,
  SettingRow,
  Spinner,
} from "@/shared/ui";
import { SettingsShell } from "@/widgets/settings-shell";

/**
 * `/settings/sessions` — the devices, and the way out of all of them.
 *
 * ## The list, at last
 *
 * A64-020.3 shipped this page with a dashed panel saying a device list was
 * not available, because `SessionService.list_user_sessions` existed and no
 * HTTP endpoint exposed it. A64-030.5C published one — `GET
 * /auth/browser/sessions` — so the panel is gone and `SessionList` renders
 * what it always should have.
 *
 * The two controls answer different questions and are deliberately not
 * merged: the list signs out *a device somebody recognises*, and the button
 * below signs out *everything, including here*. `features/sessions` owns
 * the first; this page owns the second, because only this page can navigate
 * afterwards.
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

        <SessionList />

        <SettingCard>
          <SettingRow
            label={t("profile.sessions.signOutAll")}
            description={t("profile.sessions.signOutAllHint")}
            control={
              <Dialog open={open} onOpenChange={setOpen}>
                <DialogTrigger asChild>
                  {/* Ghost with destructive text, not a red slab — the same
                      weight §18.8 gives blocking a player and removing a
                      photo. This was the only control on the page and was
                      drawn as the loudest thing in the product; a page whose
                      single action shouts reads as a warning rather than as
                      a setting. The dialog behind it is what actually
                      guards the act. */}
                  <Button
                    variant="ghost"
                    className="text-destructive hover:bg-destructive/10 hover:text-destructive min-h-11 w-full"
                  >
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
            }
          />
        </SettingCard>
      </div>
    </SettingsShell>
  );
}
