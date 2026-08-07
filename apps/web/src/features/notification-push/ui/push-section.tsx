import { usePushSection } from "@/features/notification-push/model/use-push";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Button, Spinner } from "@/shared/ui";

/**
 * The push section of `/settings/notifications` — A64-021.6 §20.
 *
 * ## Eight states, eight sentences
 *
 * §20 lists the states and says not to compress them into a boolean, and
 * the reason is that they need **different instructions**:
 *
 *     unsupported     nothing to do; this browser cannot
 *     unavailable     nothing to do; this server cannot
 *     denied          only browser settings can undo this — the page
 *                     cannot re-prompt, and offering a button that does
 *                     nothing is worse than saying so
 *     askable         press this and the browser will ask
 *     not-subscribed  permission is granted, this device is not registered
 *     muted           registered and switched off
 *     active          on
 *
 * A single disabled switch would collapse the first three, which are the
 * three somebody would file a bug about.
 *
 * ## The device count
 *
 * Shown when there is more than one, because "push is on" reads differently
 * when it is on across three browsers — and turning it off here turns it off
 * on *this* one. A person with a phone and a laptop needs to know which they
 * just changed.
 *
 * Minimal by design (§20). The final visual treatment is a later phase's;
 * what this owes now is that every state is reachable, distinguishable and
 * honest.
 */
export function PushSection({ preferenceEnabled }: { preferenceEnabled: boolean }) {
  const { t } = useTranslation();
  const push = usePushSection(preferenceEnabled);

  if (push.state === "loading") {
    return (
      <section className="flex items-center gap-2" aria-busy="true">
        <Spinner label={t("notificationPreferences.push.loading")} />
      </section>
    );
  }

  const on = push.state === "active";

  return (
    <section className="border-border flex flex-col gap-2 rounded-lg border p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h3 className="text-sm font-medium">{t("notificationPreferences.push.title")}</h3>
          <p className="text-muted-foreground text-xs">{t(EXPLANATIONS[push.state])}</p>
          {on && push.deviceCount > 1 && (
            <p className="text-muted-foreground text-xs">
              {t("notificationPreferences.push.deviceCount", { count: push.deviceCount })}
            </p>
          )}
        </div>

        {ACTIONABLE.has(push.state) && (
          <Button
            type="button"
            size="sm"
            variant={on ? "ghost" : "default"}
            className="min-h-11 shrink-0"
            disabled={push.busy}
            onClick={() => (on ? push.disable() : push.enable())}
          >
            {push.busy && <Spinner label={t("auth.common.submitting")} />}
            {t(
              on
                ? "notificationPreferences.push.disable"
                : "notificationPreferences.push.enable",
            )}
          </Button>
        )}
      </div>

      {push.failure !== null && (
        <p role="alert" className="text-destructive text-xs">
          {t(FAILURES[push.failure])}
        </p>
      )}
    </section>
  );
}

/** One sentence per state — see the component docstring on why not one. */
const EXPLANATIONS: Record<
  Exclude<ReturnType<typeof usePushSection>["state"], "loading">,
  TranslationKey
> = {
  unsupported: "notificationPreferences.push.unsupported",
  unavailable: "notificationPreferences.push.unavailable",
  denied: "notificationPreferences.push.denied",
  askable: "notificationPreferences.push.askable",
  "not-subscribed": "notificationPreferences.push.notSubscribed",
  muted: "notificationPreferences.push.muted",
  active: "notificationPreferences.push.active",
};

/**
 * The states with something to press.
 *
 * `denied` is deliberately absent: the page cannot re-prompt once somebody
 * has refused — the browser will not ask again — so a button there would do
 * nothing and teach people the feature is broken rather than that they
 * turned it off.
 */
const ACTIONABLE = new Set(["askable", "not-subscribed", "muted", "active"]);

const FAILURES: Record<"unsupported" | "denied" | "subscribe-failed", TranslationKey> = {
  unsupported: "notificationPreferences.push.unsupported",
  denied: "notificationPreferences.push.denied",
  "subscribe-failed": "notificationPreferences.push.subscribeFailed",
};
