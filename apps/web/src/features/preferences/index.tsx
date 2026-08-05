import { useId, useState } from "react";

import type { Preferences } from "@/entities/profile";
import { FormError, FormStatus } from "@/features/auth/ui/form-status";
import { profileErrorKey } from "@/features/profile/model/error-messages";
import { useUpdatePreferences } from "@/features/profile/model/queries";
import type { components } from "@/shared/api/generated/schema";
import { type Locale, LOCALES, type TranslationKey, useTranslation } from "@/shared/i18n";
import { Spinner } from "@/shared/ui";

type Schemas = components["schemas"];

/**
 * Interface and gameplay preferences.
 *
 * ## The fields are exactly the API's
 *
 * `locale{preferred_language, timezone}` and
 * `gameplay{board_theme, piece_set, confirm_move, show_coordinates,
 * animation_speed}`. Nothing is invented, and every option list comes from
 * the generated enum rather than a hand-written array — a value the server
 * would reject cannot be offered.
 *
 * ## Theme is deliberately not here
 *
 * `PreferencesResponse` has no theme field. Light/dark lives in
 * `shared/theme`, in `localStorage`, on this device. Mirroring it into the
 * backend would create two sources of truth for one setting and a
 * reconciliation question nobody asked for — so the page says so instead,
 * rather than leaving a reader to wonder where their theme went.
 *
 * ## One mutation per logical save
 *
 * `PATCH /profile/preferences` accepts a partial `{gameplay?, locale?}`, so
 * changing the board theme sends the gameplay block and nothing else. Two
 * separate endpoints would have been two round trips for one intent.
 */
export function PreferencesForm({ preferences }: { preferences: Preferences }) {
  const { t, setLocale } = useTranslation();
  const update = useUpdatePreferences();
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

  const gameplay = preferences.gameplay;

  return (
    <div className="flex flex-col gap-6">
      <FormError messageKey={failure} />
      {saved && <FormStatus>{t("profile.preferences.saved")}</FormStatus>}
      {update.isPending && (
        <p role="status" className="text-muted-foreground flex items-center gap-2 text-sm">
          <Spinner label={t("profile.preferences.save")} />
        </p>
      )}

      <Choice
        label={t("profile.preferences.language")}
        value={preferences.locale.preferred_language}
        options={LOCALES.map((code) => ({ value: code, label: code.toUpperCase() }))}
        disabled={update.isPending}
        onChange={(value) => {
          // Applied locally as well as stored: the page a person is looking
          // at should change language when they choose one, not after the
          // next reload reads it back.
          setLocale(value as Locale);
          void save({ locale: { preferred_language: value as Locale } });
        }}
      />

      <Choice
        label={t("profile.preferences.boardTheme")}
        value={gameplay.board_theme}
        options={BOARD_THEMES.map((value) => ({ value, label: value }))}
        disabled={update.isPending}
        onChange={(value) =>
          void save({ gameplay: { board_theme: value as Schemas["BoardTheme"] } })
        }
      />

      <Choice
        label={t("profile.preferences.pieceSet")}
        value={gameplay.piece_set}
        options={PIECE_SETS.map((value) => ({ value, label: value }))}
        disabled={update.isPending}
        onChange={(value) =>
          void save({ gameplay: { piece_set: value as Schemas["PieceSet"] } })
        }
      />

      <Choice
        label={t("profile.preferences.animationSpeed")}
        value={gameplay.animation_speed}
        options={ANIMATION_SPEEDS.map((value) => ({ value, label: value }))}
        disabled={update.isPending}
        onChange={(value) =>
          void save({ gameplay: { animation_speed: value as Schemas["AnimationSpeed"] } })
        }
      />

      <Toggle
        label={t("profile.preferences.confirmMove")}
        checked={gameplay.confirm_move}
        disabled={update.isPending}
        onChange={(confirm_move) => void save({ gameplay: { confirm_move } })}
      />

      <Toggle
        label={t("profile.preferences.showCoordinates")}
        checked={gameplay.show_coordinates}
        disabled={update.isPending}
        onChange={(show_coordinates) => void save({ gameplay: { show_coordinates } })}
      />

      <p className="text-muted-foreground border-border border-t pt-4 text-xs">
        {t("profile.preferences.themeNote")}
      </p>
    </div>
  );
}

/**
 * The option lists, typed **against** the generated enums.
 *
 * A union has no runtime members to iterate, so the values are written out
 * — and each array is annotated with its generated type, so a value the
 * server would reject is a compile error rather than a `422` a user finds.
 * That is not theoretical: the first draft of this file guessed `"green"`
 * and `"minimal"`, and `tsc` rejected both.
 */
const BOARD_THEMES: readonly Schemas["BoardTheme"][] = [
  "classic",
  "wood",
  "marble",
  "midnight",
];
const PIECE_SETS: readonly Schemas["PieceSet"][] = ["classic", "modern", "neo"];
const ANIMATION_SPEEDS: readonly Schemas["AnimationSpeed"][] = [
  "instant",
  "fast",
  "normal",
  "slow",
];

function Choice({
  label,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  disabled: boolean;
  onChange: (next: string) => void;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <select
        id={id}
        value={value}
        disabled={disabled}
        className="border-input focus-visible:border-ring focus-visible:ring-ring/50 min-h-11 w-full max-w-xs rounded-md border bg-transparent px-3 text-sm capitalize outline-none focus-visible:ring-[3px]"
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function Toggle({
  label,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled: boolean;
  onChange: (next: boolean) => void;
}) {
  const id = useId();
  return (
    <div className="flex items-center gap-3">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        className="accent-primary size-5"
        onChange={(event) => onChange(event.target.checked)}
      />
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
    </div>
  );
}
