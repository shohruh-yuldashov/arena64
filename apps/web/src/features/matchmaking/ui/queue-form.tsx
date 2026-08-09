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

  // The chosen control, so the action bar can say what pressing it will
  // start. Looked up rather than stored: the catalogue is the authority and
  // a second copy of the label is a second thing to keep in step.
  const chosenId = form.watch("time_control_id");
  const mode = form.watch("queue_type");
  const selected = controls.data.find((control) => control.id === chosenId);

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

      {/* A64-025.5a. The action, and where it lives at each width.
          A64-025.5 shipped one rule for both and a screenshot review showed
          why that was wrong: `bg-background` inside a `bg-card` surface is
          *darker* than its parent in dark mode — `--background` is 0.145
          and `--card` is 0.205 — so the sticky bar read as a black slab
          bolted onto the bottom of the card rather than part of it.

          Above `sm` there is no surface at all now: the action is the last
          row of the form, aligned right, on the card it already sits on.
          Below `sm` the bar stays — it is the fix that put the button in
          reach of a thumb — and it takes `bg-card`, the colour of the thing
          it is the bottom of.

          The safe-area inset is not decoration: on an iPhone the home
          indicator overlaps the last 34px of the viewport, and a submit
          button under it is one a thumb cannot reach. */}
      <div className="bg-card sticky bottom-0 -mx-4 -mb-4 flex flex-col gap-3 border-t px-4 pt-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:static sm:mx-0 sm:mb-0 sm:flex-row sm:items-center sm:justify-end sm:gap-4 sm:border-0 sm:bg-transparent sm:px-0 sm:pt-0 sm:pb-0">
        {/* Only once there is something to summarise. Before a clock is
            chosen the fieldset above already says what to do, and repeating
            it here made the emptiest state the loudest thing on the page. */}
        {selected !== undefined && (
          <p className="text-muted-foreground text-sm" aria-live="polite">
            {t("play.form.readyToPlay", {
              clock: formatTimeControl(selected, locale),
              mode: t(mode === "ranked" ? "play.mode.ranked" : "play.mode.casual"),
            })}
          </p>
        )}

        <Button
          type="submit"
          size="lg"
          className="w-full sm:w-auto sm:min-w-44"
          disabled={busy || form.watch("time_control_id") === ""}
          // Why it is disabled, for somebody who cannot see that the
          // fieldset above is untouched. Announced on demand rather than
          // rendered as a second instruction.
          aria-describedby={selected === undefined ? "queue-submit-hint" : undefined}
        >
          {join.isPending ? <Spinner label={t("play.form.joining")} /> : t("play.form.submit")}
        </Button>
        {selected === undefined && (
          <span id="queue-submit-hint" className="sr-only">
            {t("play.form.chooseClock")}
          </span>
        )}
      </div>
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
        {options.map((option) => {
          const chosen = value === option.value;
          return (
            <label
              key={option.value}
              className={cn(
                "focus-within:ring-ring relative flex min-h-11 cursor-pointer flex-col justify-center rounded-lg border px-3 py-3 transition-colors focus-within:ring-2",
                chosen
                  ? "border-primary bg-primary/10 ring-primary/30 ring-1"
                  : "border-border hover:border-primary/40 hover:bg-muted/50",
                disabled && "cursor-not-allowed opacity-60",
              )}
            >
              <input
                type="radio"
                className="sr-only"
                name={legend}
                value={option.value}
                checked={chosen}
                onChange={() => onChange(option.value)}
              />
              {/* The clock is the thing being chosen, so it is the thing
                  that is legible from across the room. `tabular-nums`
                  because `1+0` and `10+0` must not shuffle the grid. */}
              <span
                className={cn(
                  columns ? "text-lg leading-tight font-semibold" : "text-sm font-medium",
                  "tabular-nums",
                  chosen && "text-primary",
                )}
              >
                {option.label}
              </span>
              <span className="text-muted-foreground text-xs">{option.hint}</span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
