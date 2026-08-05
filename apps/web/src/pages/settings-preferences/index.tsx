import { PreferencesForm } from "@/features/preferences";
import { usePreferences } from "@/features/profile/model/queries";
import { QueryState } from "@/features/profile/ui/query-state";
import { useTranslation } from "@/shared/i18n";
import { SettingsShell } from "@/widgets/settings-shell";

/** `/settings/preferences` — locale and gameplay. Theme stays local. */
export default function SettingsPreferencesPage() {
  const { t } = useTranslation();
  const preferences = usePreferences();

  return (
    <SettingsShell
      title={t("profile.preferences.title")}
      description={t("profile.preferences.subtitle")}
    >
      <QueryState
        isPending={preferences.isPending}
        isError={preferences.isError}
        onRetry={() => void preferences.refetch()}
      >
        {preferences.data !== undefined && <PreferencesForm preferences={preferences.data} />}
      </QueryState>
    </SettingsShell>
  );
}
