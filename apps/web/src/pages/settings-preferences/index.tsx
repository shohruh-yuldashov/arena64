import { PreferencesForm } from "@/features/preferences";
import { usePreferences } from "@/features/profile/model/queries";
import { QueryState } from "@/features/profile/ui/query-state";
import { useTranslation } from "@/shared/i18n";
import { AppInstallSection } from "@/widgets/pwa";
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
      <div className="flex flex-col gap-6">
        <QueryState
          isPending={preferences.isPending}
          isError={preferences.isError}
          onRetry={() => void preferences.refetch()}
        >
          {preferences.data !== undefined && <PreferencesForm preferences={preferences.data} />}
        </QueryState>

        {/* A64-020.9 §16. Outside `QueryState` on purpose: installing the
            application is a property of this device, not of the profile
            being fetched, so it must still be offered when the preferences
            request fails. */}
        <AppInstallSection />
      </div>
    </SettingsShell>
  );
}
