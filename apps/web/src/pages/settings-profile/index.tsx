import { useMyProfile } from "@/features/profile/model/queries";
import { ProfileEditForm } from "@/features/profile/ui/edit-form";
import { QueryState } from "@/features/profile/ui/query-state";
import { useTranslation } from "@/shared/i18n";
import { SettingsShell } from "@/widgets/settings-shell";

/** `/settings/profile` — the three editable fields. */
export default function SettingsProfilePage() {
  const { t } = useTranslation();
  const profile = useMyProfile();

  return (
    <SettingsShell title={t("profile.edit.title")} description={t("profile.edit.subtitle")}>
      <QueryState
        isPending={profile.isPending}
        isError={profile.isError}
        onRetry={() => void profile.refetch()}
      >
        {/* Keyed by the loaded profile's `updated_at`-equivalent — the id
            is stable, so the form mounts once and keeps its dirty state
            across background refetches rather than resetting under the
            user's hands. */}
        {profile.data !== undefined && (
          <ProfileEditForm key={profile.data.id} profile={profile.data} />
        )}
      </QueryState>
    </SettingsShell>
  );
}
