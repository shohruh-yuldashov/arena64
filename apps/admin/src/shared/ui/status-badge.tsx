import { useTranslation, type TranslationKey } from "@/shared/i18n";

/**
 * A bounded status, rendered as a word — A64-027A §9, §31.
 *
 * Two rules, and both are about not making an operator decode the system.
 *
 * **The label is translated, never the enum.** `awaiting_acceptance` is the
 * platform's own vocabulary and it belongs on the wire; a person reading a
 * console should see "Qabul kutilmoqda". Callers pass the translation key
 * they own, so a status this component has never heard of still renders as
 * a sentence rather than as a symbol.
 *
 * **Colour is never the only signal.** The hue is an accelerant for someone
 * scanning a hundred rows; the word underneath is what carries the meaning
 * for everybody else (WCAG 1.4.1). The dot in `.status::before` survives
 * forced-colours mode, where the background tint does not.
 */

export type Tone = "neutral" | "primary" | "success" | "warning" | "danger" | "info";

export function StatusBadge({ label, tone = "neutral" }: { label: string; tone?: Tone }) {
  return (
    <span className="status" data-tone={tone === "neutral" ? undefined : tone}>
      {label}
    </span>
  );
}

/**
 * The same badge, resolving its own label from a key.
 *
 * A convenience for the common case — a column that maps one enum through
 * one namespace — so a table cell is one element rather than four lines of
 * lookup.
 */
export function StatusOf({
  value,
  namespace,
  tone,
}: {
  value: string;
  namespace: string;
  tone?: Tone;
}) {
  const { t } = useTranslation();
  return <StatusBadge label={t(`${namespace}.${value}` as TranslationKey)} tone={tone} />;
}
