import { useId, useState } from "react";

import type { PrivacyAudience, PrivacySettings } from "@/entities/profile";
import { FormError, FormStatus } from "@/features/auth/ui/form-status";
import { profileErrorKey } from "@/features/profile/model/error-messages";
import { useUpdatePrivacy } from "@/features/profile/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Spinner } from "@/shared/ui";

/**
 * Who sees what.
 *
 * ## The client never predicts the filtering
 *
 * These controls send a preference and re-read the server's answer. They do
 * **not** hide fields locally in anticipation — the public profile is
 * refetched (`useUpdatePrivacy` invalidates it) and whatever the server
 * omits is what disappears. A client that guessed would eventually guess
 * differently from the backend, and the failure mode is showing something
 * that was meant to be hidden.
 *
 * ## Why the audience settings are selects and not switches
 *
 * `online_status`, `last_seen` and `activity` are **three-valued** —
 * `everyone`, `friends`, `nobody`. A switch can only say two of those, and
 * the deprecated `show_*` booleans are exactly that lossy projection: each
 * is `true` only when its counterpart is `everyone`. Reading the booleans
 * would silently collapse a friends-only setting to "off".
 *
 * `activity` is **not** offered. The API stores it and nothing publishes
 * it, so a control for it would promise an effect that does not exist.
 *
 * ## Saved on change, not behind a button
 *
 * Each control is its own `PATCH` with its own field. A settings page with
 * a save button loses the change when somebody navigates away, and a
 * partial `PATCH` is exactly what this endpoint accepts.
 */
const AUDIENCES: PrivacyAudience[] = ["everyone", "friends", "nobody"];

export function PrivacySettingsForm({ settings }: { settings: PrivacySettings }) {
  const { t } = useTranslation();
  const update = useUpdatePrivacy();
  const [failure, setFailure] = useState<TranslationKey | null>(null);
  const [saved, setSaved] = useState(false);

  async function save(patch: Parameters<typeof update.mutateAsync>[0]): Promise<void> {
    setFailure(null);
    setSaved(false);
    try {
      await update.mutateAsync(patch);
      setSaved(true);
    } catch (error) {
      setFailure(profileErrorKey(error));
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <FormError messageKey={failure} />
      {saved && <FormStatus>{t("profile.privacy.saved")}</FormStatus>}
      {update.isPending && (
        <p role="status" className="text-muted-foreground flex items-center gap-2 text-sm">
          <Spinner label={t("profile.privacy.save")} />
        </p>
      )}

      <Toggle
        label={t("profile.privacy.showCountry")}
        description={t("profile.privacy.showCountryHint")}
        checked={settings.show_country}
        disabled={update.isPending}
        onChange={(show_country) => void save({ show_country })}
      />

      <Toggle
        label={t("profile.privacy.showStatistics")}
        description={t("profile.privacy.showStatisticsHint")}
        checked={settings.show_statistics}
        disabled={update.isPending}
        onChange={(show_statistics) => void save({ show_statistics })}
      />

      <AudienceSelect
        label={t("profile.privacy.onlineStatus")}
        description={t("profile.privacy.onlineStatusHint")}
        value={settings.online_status}
        disabled={update.isPending}
        onChange={(online_status) => void save({ online_status })}
      />

      <AudienceSelect
        label={t("profile.privacy.lastSeen")}
        description={t("profile.privacy.lastSeenHint")}
        value={settings.last_seen}
        disabled={update.isPending}
        onChange={(last_seen) => void save({ last_seen })}
      />
    </div>
  );
}

/**
 * A labelled checkbox with a described effect.
 *
 * A native `<input type="checkbox">`, not a styled `div` with
 * `role="switch"`: it is keyboard-operable, announces its state, and
 * participates in the form for free. `aria-describedby` carries the
 * *consequence* — a label that says "Show my country" without saying where
 * leaves the user guessing what they just agreed to.
 */
function Toggle({
  label,
  description,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  disabled: boolean;
  onChange: (next: boolean) => void;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-3">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-describedby={`${id}-hint`}
        className="accent-primary mt-1 size-5"
        onChange={(event) => onChange(event.target.checked)}
      />
      <div className="flex flex-col">
        <label htmlFor={id} className="text-sm font-medium">
          {label}
        </label>
        <p id={`${id}-hint`} className="text-muted-foreground text-xs">
          {description}
        </p>
      </div>
    </div>
  );
}

function AudienceSelect({
  label,
  description,
  value,
  disabled,
  onChange,
}: {
  label: string;
  description: string;
  value: PrivacyAudience;
  disabled: boolean;
  onChange: (next: PrivacyAudience) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <p id={`${id}-hint`} className="text-muted-foreground text-xs">
        {description}
      </p>
      <select
        id={id}
        value={value}
        disabled={disabled}
        aria-describedby={`${id}-hint`}
        className="border-input focus-visible:border-ring focus-visible:ring-ring/50 min-h-11 w-full max-w-xs rounded-md border bg-transparent px-3 text-sm outline-none focus-visible:ring-[3px]"
        onChange={(event) => onChange(event.target.value as PrivacyAudience)}
      >
        {AUDIENCES.map((audience) => (
          <option key={audience} value={audience}>
            {t(`profile.privacy.${audience}` as TranslationKey)}
          </option>
        ))}
      </select>
    </div>
  );
}
