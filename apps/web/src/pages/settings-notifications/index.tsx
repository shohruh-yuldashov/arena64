import { PreferenceMatrix } from "@/features/notification-preferences";
import { useNotificationPreferences } from "@/features/notification-preferences/model/queries";
import { QueryState } from "@/features/profile/ui/query-state";
import { useTranslation } from "@/shared/i18n";
import { SettingsShell } from "@/widgets/settings-shell";

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
        {preferences.data !== undefined && <PreferenceMatrix preferences={preferences.data} />}
      </QueryState>
    </SettingsShell>
  );
}
