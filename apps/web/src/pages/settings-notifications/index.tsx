import { PreferenceMatrix } from "@/features/notification-preferences";
import type { NotificationPreferences } from "@/features/notification-preferences/api";
import { useNotificationPreferences } from "@/features/notification-preferences/model/queries";
import { PushSection } from "@/features/notification-push";
import { QueryState } from "@/features/profile/ui/query-state";
import { useTranslation } from "@/shared/i18n";
import { SettingsShell } from "@/widgets/settings-shell";

/**
 * Whether any category is switched on for push.
 *
 * Read from the **matrix** rather than tracked separately, so the section
 * and the grid below it cannot disagree about a switch the person just
 * moved. One source of truth, which is the same reason the section takes
 * this as a prop instead of fetching it.
 */
function pushIsOn(preferences: NotificationPreferences): boolean {
  return preferences.settings.some((setting) => setting.channel === "push" && setting.enabled);
}

/** `/settings/notifications` — what reaches you, and where. A64-021.3 §21. */
export default function SettingsNotificationsPage() {
  const { t } = useTranslation();
  const preferences = useNotificationPreferences();

  return (
    <SettingsShell
      title={t("notificationPreferences.title")}
      description={t("notificationPreferences.subtitle")}
    >
      <QueryState
        isPending={preferences.isPending}
        isError={preferences.isError}
        onRetry={() => void preferences.refetch()}
      >
        {preferences.data !== undefined && (
          <div className="flex flex-col gap-6">
            {/* **A64-021.6 §20.** Above the matrix, and separate from it,
                because it answers a different question. The matrix is
                *what* reaches you; this is whether this browser can be
                reached at all — a device-level fact that no cell in a
                (category × channel) grid can express, and that differs
                between the phone and the laptop looking at the same
                account. */}
            <PushSection preferenceEnabled={pushIsOn(preferences.data)} />
            <PreferenceMatrix preferences={preferences.data} />
          </div>
        )}
      </QueryState>
    </SettingsShell>
  );
}
