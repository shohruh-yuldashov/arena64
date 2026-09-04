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
import { Button, Spinner } from "@/shared/ui";

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
  // The channels the server actually sent, in its order and deduplicated —
  // never a hardcoded list. A channel added on the backend appears in the
  // key without a frontend change, which is the same rule
  // `groupByCategory` follows for categories.
  const channels = useMemo(
    () =>
      [...new Set(preferences.settings.map((setting) => setting.channel))] as DeliveryChannel[],
    [preferences],
  );

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

      <ChannelKey channels={channels} />

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
        {/* `Button`, not a hand-rolled one — A64-025.9C. These two were the
            only primary and ghost controls in the product spelled out in
            utility classes, so they kept the flat brand colour when
            A64-025.9B gave `variant="default"` the brand gradient, and
            would have missed every change after it. */}
        <Button
          type="button"
          disabled={pending.size === 0 || update.isPending}
          onClick={() => void save()}
          className="min-h-11"
        >
          {update.isPending && <Spinner label={t("notificationPreferences.saving")} />}
          {update.isPending
            ? t("notificationPreferences.saving")
            : t("notificationPreferences.save")}
        </Button>

        {pending.size > 0 && !update.isPending && (
          <Button
            type="button"
            variant="ghost"
            className="min-h-11"
            onClick={() => {
              setPending(new Map());
              setFailure(null);
            }}
          >
            {t("notificationPreferences.discard")}
          </Button>
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
    <fieldset className="border-border bg-card rounded-xl border p-5">
      <legend className="px-1 text-sm font-semibold">
        {t(`notificationPreferences.categories.${category}` as TranslationKey)}
      </legend>
      <p className="text-muted-foreground text-xs">
        {t(`notificationPreferences.categoryHints.${category}` as TranslationKey)}
      </p>

      <div className="mt-4 grid gap-3 sm:grid-cols-3 sm:gap-6">
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
  // Only when it says something the key above did not — A64-025.9C.
  //
  // Every cell used to carry its channel's description, so three categories
  // × three channels printed the same three sentences nine times, and the
  // email note twice more on top. The channel's meaning does not change per
  // category, so it is stated once in `ChannelKey` and a cell is left with
  // the one thing that *is* per-cell: why this particular switch is locked.
  const lockReason = setting.locked_reason
    ? t(`notificationPreferences.locked.${setting.locked_reason}` as TranslationKey)
    : null;

  return (
    <div className="flex items-start gap-3">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled || !setting.editable}
        aria-describedby={lockReason === null ? undefined : `${id}-hint`}
        className="accent-primary mt-0.5 size-6 shrink-0"
        onChange={(event) => onChange(event.target.checked)}
      />
      <div className="flex min-w-0 flex-col">
        <label htmlFor={id} className="text-sm font-medium">
          {t(`notificationPreferences.channels.${channel}` as TranslationKey)}
        </label>
        {lockReason !== null && (
          <p id={`${id}-hint`} className="text-muted-foreground text-xs">
            {lockReason}
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * What each channel means, said once for the whole page.
 *
 * The three sentences are the same in every category — that is the point of
 * a channel — so repeating them per cell was nine copies of three facts,
 * and the email caveat two copies more. Here they are a key, above the
 * grid, in the reading order somebody meets before their first checkbox.
 *
 * Email carries its caveat here rather than beside each of its switches:
 * the address is verified or it is not, and that is a property of the
 * account, not of tournaments-versus-friends.
 */
function ChannelKey({ channels }: { channels: DeliveryChannel[] }) {
  const { t } = useTranslation();

  return (
    <div className="border-border bg-muted/30 rounded-xl border p-5">
      <h3 className="text-sm font-semibold">{t("notificationPreferences.channelKeyTitle")}</h3>
      <dl className="mt-3 grid gap-3 sm:grid-cols-3 sm:gap-6">
        {channels.map((channel) => (
          <div key={channel} className="flex flex-col gap-0.5">
            <dt className="text-sm font-medium">
              {t(`notificationPreferences.channels.${channel}` as TranslationKey)}
            </dt>
            <dd className="text-muted-foreground text-xs">
              {t(`notificationPreferences.channelHints.${channel}` as TranslationKey)}
              {channel === "email" && ` ${t("notificationPreferences.unverifiedEmailOnce")}`}
            </dd>
          </div>
        ))}
      </dl>
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
