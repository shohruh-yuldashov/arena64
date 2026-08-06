import { useTranslation } from "@/shared/i18n";
import { isIosSafari, promptInstall, useInstall, useStandaloneDisplay } from "@/shared/pwa";
import { Button } from "@/shared/ui";

/**
 * The explicit install entry — A64-020.9 §16, §32.
 *
 * `PwaNotices` offers installation once, after sign-in, and remembers a
 * "Later". This is the other half §16 asks for: a place the action is
 * *always* reachable from, so a player who dismissed the bar in March is
 * not left with no way to install in April.
 *
 * It lives on `/settings/preferences` because that is where this product
 * already puts device-scoped choices — the theme note on that page says
 * the same thing about light and dark. Installation is exactly that kind
 * of setting: it is true of this device and of no other.
 *
 * ## Every branch says something true
 *
 * A browser with no `beforeinstallprompt` and no Add to Home Screen gets a
 * sentence saying so rather than a disabled button with no explanation.
 * Saying "unavailable" is honest; a button that silently does nothing is
 * the failure §17 names — *"do not pretend installation was triggered
 * programmatically"* — in its most common form.
 */
export function AppInstallSection() {
  const { t } = useTranslation();
  const install = useInstall();
  const standalone = useStandaloneDisplay();
  const ios = isIosSafari();

  const installed = standalone || install.installed;

  return (
    <section className="border-t pt-6">
      <h2 className="text-base font-medium">{t("pwa.install.sectionTitle")}</h2>
      <p className="text-muted-foreground mt-1 text-sm">
        {t("pwa.install.sectionDescription")}
      </p>

      <div className="mt-4">
        {installed ? (
          <p className="text-sm">{t("pwa.install.installed")}</p>
        ) : install.canPrompt ? (
          <Button className="min-h-11" onClick={() => void promptInstall()}>
            {t("pwa.install.action")}
          </Button>
        ) : ios ? (
          <p className="text-sm">{t("pwa.install.iosSteps")}</p>
        ) : (
          <p className="text-muted-foreground text-sm">{t("pwa.install.unavailable")}</p>
        )}
      </div>
    </section>
  );
}
