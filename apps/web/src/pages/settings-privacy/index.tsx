import { PrivacySettingsForm } from "@/features/privacy";
import { usePrivacy } from "@/features/profile/model/queries";
import { QueryState } from "@/features/profile/ui/query-state";
import { useTranslation } from "@/shared/i18n";
import { SettingsShell } from "@/widgets/settings-shell";

/** `/settings/privacy` — what appears on the public profile. */
export default function SettingsPrivacyPage() {
  const { t } = useTranslation();
  const privacy = usePrivacy();

  return (
    <SettingsShell
      title={t("profile.privacy.title")}
      description={t("profile.privacy.subtitle")}
    >
      <QueryState
        isPending={privacy.isPending}
        isError={privacy.isError}
        onRetry={() => void privacy.refetch()}
      >
        {privacy.data !== undefined && <PrivacySettingsForm settings={privacy.data} />}
      </QueryState>
    </SettingsShell>
  );
}
