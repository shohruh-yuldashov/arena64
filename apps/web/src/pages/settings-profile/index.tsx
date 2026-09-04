import { AvatarManager } from "@/features/avatar";
import { useMyProfile } from "@/features/profile/model/queries";
import { ProfileEditForm } from "@/features/profile/ui/edit-form";
import { QueryState } from "@/features/profile/ui/query-state";
import { useTranslation } from "@/shared/i18n";
import { SettingsShell } from "@/widgets/settings-shell";

/**
 * `/settings/profile` — everything about a profile that its owner can change.
 *
 * The photo joined the three text fields in A64-025.9. It was on `/profile`,
 * which meant the avatar was drawn twice on that page and that the one
 * editable thing not reached through "Edit profile" was the one a visitor
 * sees first. Editing lives in settings; `/profile` shows the result.
 */
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
          <div className="flex flex-col gap-8">
            <AvatarManager profile={profile.data} />
            <ProfileEditForm key={profile.data.id} profile={profile.data} />
          </div>
        )}
      </QueryState>
    </SettingsShell>
  );
}
