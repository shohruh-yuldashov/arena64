import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import {
  formatTimeControl,
  type TimeControl,
  type TimeControlId,
} from "@/entities/time-control";
import { FormError } from "@/features/auth/ui/form-status";
import { cooldownSeconds, queueErrorKey } from "@/features/matchmaking/model/error-messages";
import { useJoinQueue, useTimeControls } from "@/features/matchmaking/model/queries";
import { type TranslationKey, useTranslation } from "@/shared/i18n";
import { cn } from "@/shared/lib/cn";
import { Button, Skeleton, Spinner } from "@/shared/ui";

/**
 * Choose a game, and join a pool — A64-020.5A §4, §5, §6.
 *
 * ## Two choices, and the two that are deliberately absent
 *
 * Mode and clock. Neither **variant** nor **region** is offered, and each
 * omission is a product decision rather than a gap.
 *
 * `ProductVariant` has exactly one member. A radio group with one option is
 * not a choice — it is a control that can only be left where it was — and
 * building one would mean enumerating the variants in this file, which is
 * the hardcoded list §4 rules out for the catalogue and which would be no
 * better here. The request omits the field and the server applies its
 * default. A second variant makes this a fieldset beside the other two.
 *
 * `Region` exists on the request and defaults to `global`, but AD-25 defers
 * multi-region *infrastructure* and there is one deployment — so every
 * non-default value strictly shrinks the pool a player is matched from and
 * buys no latency back. A picker offering seven regions on a single-region
 * platform is a control whose only possible effect is to make matchmaking
 * worse.
 *
 * ## The clock has no default
 *
 * `time_control_id` starts unselected and submit stays disabled until one
 * is chosen. Every control is a genuinely different game — the backend
 * refuses to default it for exactly that reason (A64-020.5A-pre §16) — so
 * pre-selecting one here would put a player into a pool they did not pick
 * and would make the platform's most consequential choice the one that took
 * the fewest clicks.
 *
 * Mode *does* default, because it has a real one: `casual` is the safer of
 * the two to land on for somebody who has not read the labels, since it is
 * the one whose result does not move a permanent number.
 *
 * ## Nothing is hardcoded
 *
 * The controls are the catalogue's, fetched. §4 forbids both hardcoding the
 * four and inferring durations from an identifier, and this component could
 * not do either: it renders what the server sent and submits the `id` it
 * came with.
 */

const schema = z.object({
  queue_type: z.enum(["ranked", "casual"]),
  // `.min(1)` rather than an enum of the four codes: the catalogue is the
  // server's, and a client-side enum would be the hardcoded list §4
  // forbids. What this validates is "you picked something", which is the
  // only claim this form is entitled to make about it.
  time_control_id: z.string().min(1),
});

type QueueFormValues = z.infer<typeof schema>;

export function QueueForm({ disabled = false }: { disabled?: boolean }) {
  const { t, locale } = useTranslation();
  const controls = useTimeControls();
  const join = useJoinQueue();
  const [failure, setFailure] = useState<TranslationKey | null>(null);
  const [cooldown, setCooldown] = useState<number | null>(null);

  const form = useForm<QueueFormValues>({
    resolver: zodResolver(schema),
    defaultValues: { queue_type: "casual", time_control_id: "" },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    setFailure(null);
    setCooldown(null);
    try {
      await join.mutateAsync({
        // `variant` and `region` are on the request and are **not** on the
        // form — see this component's docstring on why neither is a choice
        // worth offering today. They are sent explicitly at their product
        // defaults rather than omitted, because the generated request type
        // requires them: Pydantic defaults them server-side, and a client
        // that relied on that would be typing around its own contract.
        variant: "russian_8x8",
        region: "global",
        queue_type: values.queue_type,
        // The catalogue's own identifier, submitted verbatim. Nothing here
        // sends `base_time_ms` — §4, and the endpoint would refuse it.
        //
        // The cast is the one place a runtime string becomes the generated
        // union, and it is sound because the value can only have come from
        // `controls.data`: this form renders no option it did not receive.
        // Validating it against a client-side enum instead would be the
        // hardcoded list §4 forbids.
        time_control_id: values.time_control_id as TimeControlId,
      });
    } catch (error) {
      setFailure(queueErrorKey(error, "queue"));
      setCooldown(cooldownSeconds(error));
    }
  });

  if (controls.isPending) {
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  if (controls.isError || controls.data === undefined || controls.data.length === 0) {
    // An empty catalogue is not "no games available" — the four controls are
    // seeded by the migration that creates the table, so an empty list means
    // the deployment is not migrated. Both read as "we cannot start a game
    // right now", which is the honest thing to say to a player either way.
    return <FormError messageKey="play.errors.catalogue_unavailable" />;
  }

  // `join.isPending` covers the request; `disabled` covers "the lobby is
  // busy for a reason this form cannot see" — a cancel in flight, a session
  // that has stopped resolving. Both must stop a second submit, which is
  // §6's "no duplicate join requests".
  const busy = join.isPending || disabled;

  return (
    <form onSubmit={(event) => void onSubmit(event)} className="flex flex-col gap-6" noValidate>
      <Controller
        control={form.control}
        name="queue_type"
        render={({ field }) => (
          <RadioGroup
            legend={t("play.form.mode")}
            hint={t("play.form.modeHint")}
            value={field.value}
            onChange={field.onChange}
            disabled={busy}
            options={[
              {
                value: "casual",
                label: t("play.mode.casual"),
                hint: t("play.mode.casualHint"),
              },
              {
                value: "ranked",
                label: t("play.mode.ranked"),
                hint: t("play.mode.rankedHint"),
              },
            ]}
          />
        )}
      />

      <Controller
        control={form.control}
        name="time_control_id"
        render={({ field }) => (
          <RadioGroup
            legend={t("play.form.timeControl")}
            hint={t("play.form.timeControlHint")}
            value={field.value}
            onChange={field.onChange}
            disabled={busy}
            columns
            options={controls.data.map((control) => ({
              value: control.id,
              label: formatTimeControl(control, locale),
              hint: t(SPEED_LABELS[control.speed_class] ?? "play.speed.unknown"),
            }))}
          />
        )}
      />

      {failure !== null && <FormError messageKey={failure} />}
      {cooldown !== null && (
        <p className="text-muted-foreground text-sm" role="status">
          {t("play.errors.cooldownSeconds", { seconds: String(cooldown) })}
        </p>
      )}

      <Button
        type="submit"
        size="lg"
        className="min-h-11"
        disabled={busy || form.watch("time_control_id") === ""}
      >
        {join.isPending ? <Spinner label={t("play.form.joining")} /> : t("play.form.submit")}
      </Button>
    </form>
  );
}

/**
 * The catalogue's speed classes, as translated labels.
 *
 * A lookup rather than a translation key built from the value, so a class
 * the client has no string for renders "unknown" instead of the raw code —
 * and so `npm run typecheck` catches a missing translation rather than
 * production catching it.
 */
const SPEED_LABELS: Partial<Record<TimeControl["speed_class"], TranslationKey>> = {
  bullet: "play.speed.bullet",
  blitz: "play.speed.blitz",
  rapid: "play.speed.rapid",
  classical: "play.speed.classical",
  correspondence: "play.speed.correspondence",
};

/**
 * An accessible radio group — §23.
 *
 * `fieldset`/`legend` rather than a `div` with an `aria-label`, because a
 * legend is what makes a screen reader announce "Time control, 3+2, radio
 * button 2 of 4" instead of four unrelated controls. Native `input
 * type="radio"` rather than a Radix group: the browser's own arrow-key
 * behaviour, roving tab index and form semantics are exactly right here and
 * re-implementing them buys nothing but a chance to get one wrong.
 *
 * The visual selection is a border and a background; the `sr-only` radio is
 * still the accessible control, so state is never communicated by colour
 * alone (WCAG 1.4.1) — the checked input is what assistive technology reads.
 */
function RadioGroup({
  legend,
  hint,
  value,
  onChange,
  options,
  disabled,
  columns = false,
}: {
  legend: string;
  hint: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string; hint: string }[];
  disabled: boolean;
  columns?: boolean;
}) {
  return (
    <fieldset disabled={disabled} className="min-w-0">
      <legend className="text-sm font-medium">{legend}</legend>
      <p className="text-muted-foreground mt-1 text-xs">{hint}</p>
      <div
        className={cn(
          "mt-3 grid gap-2",
          columns ? "grid-cols-2 sm:grid-cols-4" : "grid-cols-1 sm:grid-cols-2",
        )}
      >
        {options.map((option) => (
          <label
            key={option.value}
            className={cn(
              "border-border focus-within:ring-ring flex min-h-11 cursor-pointer flex-col justify-center rounded-md border px-3 py-2 focus-within:ring-2",
              value === option.value && "border-primary bg-primary/5",
              disabled && "cursor-not-allowed opacity-60",
            )}
          >
            <input
              type="radio"
              className="sr-only"
              name={legend}
              value={option.value}
              checked={value === option.value}
              onChange={() => onChange(option.value)}
            />
            <span className="text-sm font-medium tabular-nums">{option.label}</span>
            <span className="text-muted-foreground text-xs">{option.hint}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
