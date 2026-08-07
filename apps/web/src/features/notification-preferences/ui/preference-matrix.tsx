import { useId, useMemo, useState } from "react";

import { FormError, FormStatus } from "@/features/auth/ui/form-status";
import type {
  DeliveryChannel,
  NotificationCategory,
  NotificationPreferences,
  PreferenceChange,
  PreferenceSetting,
} from "@/features/notification-preferences/api";
import { preferenceErrorKey } from "@/features/notification-preferences/model/error-messages";
import { useUpdateNotificationPreferences } from "@/features/notification-preferences/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { Spinner } from "@/shared/ui";

/**
 * The `(category, channel)` matrix — A64-021.3 §21, §22.
 *
 * ## Grouped by category, not rendered as a table
 *
 * A `<table>` of four rows by three columns reads well at 1200px and badly
 * at 360px, where a header cell and its checkbox end up on different
 * screens. Each category is its own `<fieldset>` with the channels inside
 * it, which stacks naturally and — more importantly — gives a screen reader
 * the grouping for free: the legend is announced before every control it
 * contains, so "Email" is never heard without knowing email *of what*.
 *
 * ## An explanation, never a bare disabled control
 *
 * §7 gives four independent facts per cell and this renders all four. A
 * control a player cannot use always says why, in their language, from
 * `locked_reason` — a greyed-out switch with no explanation is worse than
 * an absent one, and the two reasons are genuinely different sentences:
 * *always on because we must be able to reach you* and *not built yet*.
 *
 * The reason text is wired through `aria-describedby`, so it is part of the
 * control's accessible description rather than nearby text a screen reader
 * would read separately, if at all.
 *
 * ## Explicit save, unlike the privacy form
 *
 * `features/privacy` saves on change, and that is right there: each control
 * is its own independent `PATCH` with no cross-field rule. Here one illegal
 * change rejects the **whole** batch (§9), so a per-toggle save would leave
 * a player unable to tell which of their changes was refused — and a
 * refusal would arrive after they had already moved on to the next switch.
 *
 * So the pending changes accumulate locally and are sent together. The
 * count of unsaved changes is shown rather than a bare dot, and `Discard`
 * exists because the only other way out of a half-made decision is to
 * reload the page.
 *
 * ## The client does not predict the outcome
 *
 * No optimistic update. The response of the save *is* the new state (§17),
 * so what the screen shows after a save is what the server stored — never
 * what was asked for. A consent control that flipped and then flipped back
 * would be the one place this platform should not guess.
 */
export function PreferenceMatrix({ preferences }: { preferences: NotificationPreferences }) {
  const { t } = useTranslation();
  const update = useUpdateNotificationPreferences();
  const [pending, setPending] = useState<ReadonlyMap<string, boolean>>(new Map());
  const [failure, setFailure] = useState<TranslationKey | null>(null);
  const [saved, setSaved] = useState(false);

  const categories = useMemo(() => groupByCategory(preferences.settings), [preferences]);

  function toggle(setting: PreferenceSetting, next: boolean): void {
    setSaved(false);
    setPending((current) => {
      const draft = new Map(current);
      const key = cellKey(setting);
      // Toggling back to the stored value removes the change rather than
      // recording a no-op: otherwise "2 unsaved changes" would count two
      // switches that are both exactly where they started.
      if (next === setting.enabled) draft.delete(key);
      else draft.set(key, next);
      return draft;
    });
  }

  async function save(): Promise<void> {
    setFailure(null);
    setSaved(false);
    try {
      await update.mutateAsync(changesOf(pending));
      setPending(new Map());
      setSaved(true);
    } catch (error) {
      // The pending changes are **kept** on failure. A refusal names one
      // pair; discarding the whole batch would throw away the legal changes
      // the player made alongside it and make them do the work twice.
      setFailure(preferenceErrorKey(error));
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <FormError messageKey={failure} />
      {saved && <FormStatus>{t("notificationPreferences.saved")}</FormStatus>}

      {categories.map(([category, settings]) => (
        <CategoryGroup
          key={category}
          category={category}
          settings={settings}
          pending={pending}
          disabled={update.isPending}
          onToggle={toggle}
        />
      ))}

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={pending.size === 0 || update.isPending}
          onClick={() => void save()}
          className="bg-primary text-primary-foreground focus-visible:ring-ring inline-flex min-h-11 items-center gap-2 rounded-md px-4 text-sm font-medium disabled:opacity-50 focus-visible:ring-2 focus-visible:outline-none"
        >
          {update.isPending && <Spinner label={t("notificationPreferences.saving")} />}
          {update.isPending
            ? t("notificationPreferences.saving")
            : t("notificationPreferences.save")}
        </button>

        {pending.size > 0 && !update.isPending && (
          <button
            type="button"
            onClick={() => {
              setPending(new Map());
              setFailure(null);
            }}
            className="text-muted-foreground hover:text-foreground focus-visible:ring-ring min-h-11 rounded-md px-2 text-sm focus-visible:ring-2 focus-visible:outline-none"
          >
            {t("notificationPreferences.discard")}
          </button>
        )}

        {pending.size > 0 && (
          // `role="status"` rather than a silent count: a keyboard user who
          // has toggled three switches should be told there is something to
          // save without having to tab to the button to find out.
          <p role="status" className="text-muted-foreground text-sm">
            {t("notificationPreferences.unsaved", { count: pending.size })}
          </p>
        )}
      </div>
    </div>
  );
}

function CategoryGroup({
  category,
  settings,
  pending,
  disabled,
  onToggle,
}: {
  category: NotificationCategory;
  settings: PreferenceSetting[];
  pending: ReadonlyMap<string, boolean>;
  disabled: boolean;
  onToggle: (setting: PreferenceSetting, next: boolean) => void;
}) {
  const { t } = useTranslation();

  return (
    <fieldset className="border-border rounded-lg border p-4">
      <legend className="px-1 text-sm font-medium">
        {t(`notificationPreferences.categories.${category}` as TranslationKey)}
      </legend>
      <p className="text-muted-foreground text-xs">
        {t(`notificationPreferences.categoryHints.${category}` as TranslationKey)}
      </p>

      <div className="mt-4 flex flex-col gap-4 sm:flex-row sm:gap-8">
        {settings.map((setting) => (
          <ChannelToggle
            key={setting.channel}
            setting={setting}
            checked={pending.get(cellKey(setting)) ?? setting.enabled}
            disabled={disabled}
            onChange={(next) => onToggle(setting, next)}
          />
        ))}
      </div>
    </fieldset>
  );
}

/**
 * One cell.
 *
 * A native `<input type="checkbox">`, not a styled `div` with
 * `role="switch"`: it is keyboard-operable, announces its own state and
 * participates in the form for free — the same choice `features/privacy`
 * made and for the same reasons.
 */
function ChannelToggle({
  setting,
  checked,
  disabled,
  onChange,
}: {
  setting: PreferenceSetting;
  checked: boolean;
  disabled: boolean;
  onChange: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const channel = setting.channel as DeliveryChannel;
  // The lock's reason when there is one, and otherwise what the channel
  // *is* — A64-021.5 §26. The hints deliberately describe the channel
  // rather than its availability: "not available yet" belongs to
  // `locked.channel_unavailable`, which the server sends only while it is
  // true. A hint that said it unconditionally would keep saying it the day
  // email starts working, which is the same lie in the other direction.
  const hint = setting.locked_reason
    ? t(`notificationPreferences.locked.${setting.locked_reason}` as TranslationKey)
    : t(`notificationPreferences.channelHints.${channel}` as TranslationKey);

  return (
    <div className="flex items-start gap-3">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled || !setting.editable}
        aria-describedby={`${id}-hint`}
        className="accent-primary mt-1 size-5"
        onChange={(event) => onChange(event.target.checked)}
      />
      <div className="flex flex-col">
        <label htmlFor={id} className="text-sm font-medium">
          {t(`notificationPreferences.channels.${channel}` as TranslationKey)}
        </label>
        <p id={`${id}-hint`} className="text-muted-foreground text-xs">
          {hint}
        </p>
        {channel === "email" && setting.editable && (
          // Said only where it applies. Email reaches a **verified**
          // address and nothing else (§6), and a player whose address is
          // unconfirmed would otherwise turn a switch on and receive
          // nothing with no explanation anywhere.
          <p className="text-muted-foreground text-xs">
            {t("notificationPreferences.notes.unverifiedEmail")}
          </p>
        )}
      </div>
    </div>
  );
}

function cellKey(setting: PreferenceSetting): string {
  return `${setting.category}:${setting.channel}`;
}

function changesOf(pending: ReadonlyMap<string, boolean>): PreferenceChange[] {
  return [...pending].map(([key, enabled]) => {
    const [category, channel] = key.split(":");
    return {
      category: category as NotificationCategory,
      channel: channel as DeliveryChannel,
      enabled,
    };
  });
}

/**
 * The settings grouped by category, in the order the server sent them.
 *
 * Server order rather than a hardcoded list: the backend orders by its own
 * enum declarations, so a category added there appears here without a
 * frontend change — and a client with its own order would silently drop
 * the new one.
 */
function groupByCategory(
  settings: PreferenceSetting[],
): [NotificationCategory, PreferenceSetting[]][] {
  const grouped = new Map<NotificationCategory, PreferenceSetting[]>();
  for (const setting of settings) {
    const category = setting.category as NotificationCategory;
    const existing = grouped.get(category);
    if (existing) existing.push(setting);
    else grouped.set(category, [setting]);
  }
  return [...grouped];
}
